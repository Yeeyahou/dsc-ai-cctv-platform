"""Development-only file receiver; it never creates an AWS/backend client."""

from __future__ import annotations

import argparse
import fcntl
import json
import signal
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from dsc.edge.config import EdgeSettings
from dsc.edge.inbox import receive_inbox
from dsc.edge.replication import recover_unstaged
from dsc.edge.store import EdgeStore
from dsc.shared.observability import configure_logging, emit_event, logged


def local_settings(root: Path) -> EdgeSettings:
    root = root.resolve()
    return EdgeSettings(
        _env_file=None,
        environment="local",
        role="field",
        server_id="local-field-test",
        site_id="local-test-site",
        api_url="https://unused.invalid",
        token_url="https://unused.invalid/token",
        client_id="local-files-only",
        secret_file=root / "unused-secret-not-created",
        nas_root=root / "input-clips",
        inbox=root / "inbox",
        state_dir=root / "state",
        cache_root=root / "managed-clips",
        archive_allowed_hosts=["unused.invalid"],
    )


@logged()
def snapshot(root: Path) -> dict:
    """Read state without resetting an active receiver's SENDING records."""
    path = local_settings(root).state_dir / "outbox.sqlite"
    if not path.exists():
        return {"ready": False, "outbox": [], "copies": []}
    with closing(sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        return {
            "ready": True,
            "outbox": [dict(row) for row in db.execute("SELECT * FROM outbox ORDER BY id")],
            "copies": [
                dict(row)
                for row in db.execute("SELECT * FROM clip_copies ORDER BY event_id,revision")
            ],
        }


@logged()
def receive(root: Path, stop: threading.Event, *, once: bool = False) -> None:
    settings = local_settings(root)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.nas_root.mkdir(parents=True, exist_ok=True)
    with (settings.state_dir / "edge-server.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = EdgeStore(settings.state_dir / "outbox.sqlite")
        emit_event("local_field_receiver_started", cloudSendingEnabled=False)
        while not stop.is_set():
            receive_inbox(settings, store)
            recover_unstaged(store, settings)
            if once:
                return
            stop.wait(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    configure_logging("field-local")
    if args.inspect:
        print(json.dumps(snapshot(args.root)))
    else:
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        receive(args.root, stop, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
