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

import httpx

from dsc import __version__
from dsc.edge.client import EdgeClient
from dsc.edge.config import EdgeSettings
from dsc.edge.inbox import receive_inbox
from dsc.edge.store import EdgeStore
from dsc.edge.worker import run_job, send_pending
from dsc.shared.contracts import iso, now_ms
from dsc.shared.observability import configure_logging, contextual, emit_event, logged


@logged(fields={"agentId": "settings.agent_id", "siteId": "settings.site_id"})
def heartbeat(settings: EdgeSettings, store: EdgeStore) -> dict:
    count, oldest = store.stats()
    try:
        list(settings.nas_root.iterdir())
        nas_status = "OK"
    except FileNotFoundError:
        nas_status = "UNREACHABLE"
    except OSError:
        nas_status = "READ_ERROR"
    result = {
        "schemaVersion": "1.0",
        "agentVersion": __version__,
        "observedAt": iso(now_ms()),
        "outboxPendingCount": count,
        "diskFreeBytes": shutil.disk_usage(settings.state_dir).free,
        "nasStatus": nas_status,
    }
    if oldest is not None:
        result["oldestPendingAt"] = iso(oldest)
    return result


@logged(fields={"agentId": "settings.agent_id", "siteId": "settings.site_id"})
def serve(settings: EdgeSettings, once=False) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    # Only one process may restore SENDING entries and claim this installation's outbox.
    with (settings.state_dir / "agent.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = EdgeStore(settings.state_dir / "outbox.sqlite")
        client = EdgeClient(settings)
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        future = None
        next_heartbeat = next_claim = 0
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-upload") as workers:
                while not stop.is_set():
                    try:
                        receive_inbox(settings, store)
                        for _ in range(100):
                            if not send_pending(client, store, settings):
                                break
                    except (OSError, ValueError):
                        emit_event("local_reception_failed", level=logging.ERROR)
                    now = time.monotonic()
                    try:
                        if now >= next_heartbeat:
                            next_heartbeat = now + settings.heartbeat_seconds
                            client.post("/agent/heartbeat", heartbeat(settings, store))
                        if future and future.done():
                            future.result()
                            future = None
                        if future is None and now >= next_claim:
                            job = client.post("/agent/jobs/claim", {})
                            next_claim = now + settings.claim_seconds
                            if job:
                                future = workers.submit(
                                    contextual(run_job, client, store, settings, job)
                                )
                    except (httpx.HTTPError, OSError, ValueError, KeyError):
                        emit_event("agent_api_temporarily_unavailable", level=logging.WARNING)
                        next_claim = now + settings.claim_seconds
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
    parser = argparse.ArgumentParser(description="DSC outbound edge agent")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inspect-outbox", action="store_true")
    parser.add_argument("--retry", type=int, metavar="OUTBOX_ID")
    args = parser.parse_args()
    settings = EdgeSettings(_env_file=args.env_file)
    configure_logging("edge", settings.log_level)
    if args.inspect_outbox or args.retry is not None:
        # Operator commands use the same process lock; they must not reset an active sender.
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with (settings.state_dir / "agent.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store = EdgeStore(settings.state_dir / "outbox.sqlite")
            if args.retry is not None:
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
