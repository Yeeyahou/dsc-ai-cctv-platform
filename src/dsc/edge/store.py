from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dsc.shared.contracts import EventEnvelope, MediaEnvelope, canonical, digest, now_ms
from dsc.shared.observability import emit_event, log_methods, logged

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    identity TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, accepted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL,
    body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
    accepted_at INTEGER NOT NULL, last_error TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(state, next_at, id);
CREATE TABLE IF NOT EXISTS manifests (
    clip_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, relative_path TEXT NOT NULL,
    revision INTEGER NOT NULL
);
"""


@log_methods(level=logging.DEBUG)
class EdgeStore:
    def __init__(self, path: Path, clock=now_ms):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("UPDATE outbox SET state='PENDING' WHERE state='SENDING'")

    @contextmanager
    @logged(level=logging.DEBUG)
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @logged(fields={"eventId": "envelope.event.event_id"})
    def accept(self, envelope: EventEnvelope | MediaEnvelope) -> bool:
        event = isinstance(envelope, EventEnvelope)
        event_id = str(envelope.event.event_id if event else envelope.event_id)
        revision = 0 if event else envelope.media.revision
        identity = f"{event_id}:{'event' if event else 'media'}:{revision}"
        body = envelope.event.wire() if event else envelope.media.wire()
        fingerprint = digest(envelope.wire())
        with self.connect() as db:
            prior = db.execute(
                "SELECT payload_hash FROM receipts WHERE identity=?", (identity,)
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError("same local identity has a different body")
                return False
            if envelope.local_media:
                media = envelope.local_media
                prior_path = db.execute(
                    "SELECT * FROM manifests WHERE clip_id=?", (str(media.clip_id),)
                ).fetchone()
                if prior_path and (
                    prior_path["event_id"] != event_id
                    or prior_path["relative_path"] != media.relative_path
                ):
                    raise ValueError("clip mapping is immutable")
                db.execute(
                    """INSERT INTO manifests(clip_id,event_id,relative_path,revision)
                              VALUES(?,?,?,?) ON CONFLICT(clip_id) DO UPDATE
                              SET revision=MAX(revision,excluded.revision)""",
                    (str(media.clip_id), event_id, media.relative_path, revision),
                )
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (identity, fingerprint, self.clock()))
            db.execute(
                """INSERT INTO outbox(identity,event_id,kind,revision,body,accepted_at)
                          VALUES(?,?,?,?,?,?)""",
                (identity, event_id, envelope.kind, revision, canonical(body), self.clock()),
            )
        emit_event("outbox_accepted", eventId=event_id)
        return True

    def next_item(self) -> dict | None:
        with self.connect() as db:
            # Media updates cannot overtake the event ACK or an earlier unresolved media revision.
            row = db.execute(
                """SELECT o.* FROM outbox o WHERE o.state='PENDING' AND o.next_at<=?
                AND (o.kind='event' OR (
                    EXISTS(SELECT 1 FROM outbox e WHERE e.event_id=o.event_id
                           AND e.kind='event' AND e.state='ACKED')
                    AND NOT EXISTS(SELECT 1 FROM outbox m WHERE m.event_id=o.event_id
                           AND m.kind='media' AND m.revision<o.revision AND m.state!='ACKED')
                )) ORDER BY o.id LIMIT 1""",
                (self.clock(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE outbox SET state='SENDING' WHERE id=? AND state='PENDING'", (row["id"],)
            )
            return dict(row)

    def mark(self, item_id: int, state: str, delay_ms=0, code: str | None = None) -> None:
        if state not in {"PENDING", "ACKED", "QUARANTINED"}:
            raise ValueError("invalid outbox state")
        with self.connect() as db:
            db.execute(
                """UPDATE outbox SET state=?,next_at=?,attempts=attempts+1,last_error=?
                          WHERE id=?""",
                (state, self.clock() + delay_ms, code, item_id),
            )
        emit_event(
            "outbox_state_changed",
            level=logging.WARNING if code else logging.INFO,
            outboxId=item_id,
            state=state,
            retryMs=delay_ms,
            errorCode=code,
        )

    @logged()
    def retry_quarantined(self, item_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET state='PENDING',next_at=0,last_error=NULL "
                "WHERE id=? AND state='QUARANTINED'",
                (item_id,),
            )

    def manifest(self, clip_id: str, event_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM manifests WHERE clip_id=? AND event_id=?", (clip_id, event_id)
            ).fetchone()
            return dict(row) if row else None

    def stats(self) -> tuple[int, int | None]:
        with self.connect() as db:
            count, oldest = db.execute(
                "SELECT COUNT(*), MIN(accepted_at) FROM outbox WHERE state!='ACKED'"
            ).fetchone()
            return count, oldest

    def rows(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox ORDER BY id")]
