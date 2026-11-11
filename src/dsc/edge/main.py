import argparse
import fcntl
import json
import logging
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx

from dsc import __version__
from dsc.edge.client import EdgeClient
from dsc.edge.config import EdgeSettings
from dsc.edge.files import check_volume
from dsc.edge.inbox import receive_inbox
from dsc.edge.replication import recover_unstaged, replicate_pending
from dsc.edge.store import EdgeStore
from dsc.edge.worker import run_job, send_pending
from dsc.shared.contracts import iso, now_ms
from dsc.shared.observability import configure_logging, contextual, emit_event, logged


@logged(fields={"edgeServerId": "settings.server_id", "siteId": "settings.site_id"})
def heartbeat(settings: EdgeSettings, store: EdgeStore) -> dict:
    count, oldest = store.stats()
    try:
        if settings.role == "archive":
            check_volume(settings.nas_root, settings.archive_volume_id)
        else:
            list((settings.cache_root if settings.role == "field" else settings.nas_root).iterdir())
        nas_status = "OK"
    except FileNotFoundError:
        nas_status = "UNREACHABLE"
    except OSError:
        nas_status = "READ_ERROR"
    disk_root = (
        settings.nas_root
        if settings.role == "archive"
        else settings.cache_root
        if settings.role == "field"
        else settings.state_dir
    )
    try:
        free = min(shutil.disk_usage(disk_root).free, shutil.disk_usage(settings.state_dir).free)
    except OSError:
        free = 0
    if free < settings.disk_warning_bytes:
        emit_event("clip_disk_space_low", level=logging.ERROR, diskFreeBytes=free)
    result = {
        "schemaVersion": "1.1",
        "edgeServerVersion": __version__,
        "observedAt": iso(now_ms()),
        "outboxPendingCount": count,
        "diskFreeBytes": free,
        "nasStatus": nas_status,
    }
    if settings.role == "field":
        copies = store.copy_rows()
        result["replicaPendingCount"] = sum(row["state"] in {"LOCAL", "RETIRING"} for row in copies)
        result["replicaQuarantinedCount"] = sum(row["state"] == "QUARANTINED" for row in copies)
    if oldest is not None:
        result["oldestPendingAt"] = iso(oldest)
    return result


@logged(fields={"edgeServerId": "settings.server_id", "siteId": "settings.site_id"})
def serve(settings: EdgeSettings, once=False) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    # Only one process may restore SENDING entries and claim this installation's outbox.
    with (settings.state_dir / "edge-server.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = EdgeStore(settings.state_dir / "outbox.sqlite")
        if settings.role == "field":
            settings.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        client = EdgeClient(settings)
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        futures = {}
        next_heartbeat = next_claim = next_replication = 0

        def process_inputs():
            receive_inbox(settings, store)
            if settings.role == "field":
                recover_unstaged(store, settings)
            for _ in range(10):
                if stop.is_set() or not send_pending(client, store, settings):
                    break

        try:
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="edge-work") as workers:
                while not stop.is_set():
                    for name, future in list(futures.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception as exc:
                                emit_event(
                                    "edge_background_pending",
                                    level=logging.ERROR,
                                    operation=name,
                                    errorType=type(exc).__name__,
                                )
                            del futures[name]
                    if settings.role != "archive" and "inputs" not in futures:
                        futures["inputs"] = workers.submit(contextual(process_inputs))
                    now = time.monotonic()
                    try:
                        if now >= next_heartbeat:
                            next_heartbeat = now + settings.heartbeat_seconds
                            client.post("/edge-server/heartbeat", heartbeat(settings, store))
                        if (
                            settings.role == "field"
                            and "replication" not in futures
                            and now >= next_replication
                        ):
                            next_replication = now + settings.replication_seconds
                            futures["replication"] = workers.submit(
                                contextual(replicate_pending, client, store, settings)
                            )
                        if "upload" not in futures and now >= next_claim:
                            next_claim = now + settings.claim_seconds
                            job = client.post("/edge-server/jobs/claim", {})
                            if job:
                                futures["upload"] = workers.submit(
                                    contextual(run_job, client, store, settings, job)
                                )
                    except (httpx.HTTPError, OSError, ValueError, KeyError):
                        emit_event("edge_server_api_temporarily_unavailable", level=logging.WARNING)
                    if once:
                        break
                    stop.wait(1)
        finally:
            client.close()


def main() -> int:
    configure_logging("edge")
    return run_cli()


@logged()
def run_cli() -> int:
    parser = argparse.ArgumentParser(description="DSC outbound edge server")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inspect-outbox", action="store_true")
    parser.add_argument("--inspect-copies", action="store_true")
    parser.add_argument("--retry-copy", nargs=2, metavar=("EVENT_ID", "REVISION"))
    parser.add_argument("--retry", type=int, metavar="OUTBOX_ID")
    args = parser.parse_args()
    settings = EdgeSettings(_env_file=args.env_file)
    configure_logging("edge", settings.log_level)
    if args.inspect_outbox or args.inspect_copies or args.retry_copy or args.retry is not None:
        # Operator commands use the same process lock; they must not reset an active sender.
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with (settings.state_dir / "edge-server.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store = EdgeStore(settings.state_dir / "outbox.sqlite")
            if args.retry_copy:
                event_id = str(UUID(args.retry_copy[0]))
                revision = int(args.retry_copy[1])
                if revision < 0:
                    raise ValueError("revision must be nonnegative")
                store.retry_copy(event_id, revision)
            elif args.inspect_copies:
                for row in store.copy_rows():
                    print(
                        json.dumps(
                            {
                                key: row[key]
                                for key in (
                                    "event_id",
                                    "clip_id",
                                    "revision",
                                    "state",
                                    "size_bytes",
                                    "attempts",
                                )
                            }
                        )
                    )
            elif args.retry is not None:
                store.retry_quarantined(args.retry)
            else:
                for row in store.rows():
                    print(
                        json.dumps(
                            {
                                key: row[key]
                                for key in (
                                    "id",
                                    "event_id",
                                    "kind",
                                    "revision",
                                    "state",
                                    "attempts",
                                    "last_error",
                                )
                            }
                        )
                    )
    else:
        serve(settings, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
