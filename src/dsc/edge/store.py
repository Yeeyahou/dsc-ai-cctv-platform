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
CREATE TABLE IF NOT EXISTS copy_recovery (
    event_id TEXT NOT NULL, revision INTEGER NOT NULL, next_at INTEGER NOT NULL,
    PRIMARY KEY(event_id, revision)
);
CREATE TABLE IF NOT EXISTS clip_copies (
    event_id TEXT NOT NULL, clip_id TEXT NOT NULL, revision INTEGER NOT NULL,
    relative_path TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL, checksum TEXT NOT NULL,
    created_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'LOCAL', receipt TEXT,
    next_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(event_id, revision)
);
"""


@log_methods(level=logging.DEBUG)
class EdgeStore:
    def __init__(self, path: Path, clock=now_ms, recover_sending=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        with self.connect() as db:
            db.executescript(SCHEMA)
            if recover_sending:
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
    def accept(
        self, envelope: EventEnvelope | MediaEnvelope, staged_copy: dict | None = None
    ) -> bool:
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
            if staged_copy:
                self.save_copy(staged_copy, db=db)
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

    def save_copy(self, value: dict, db: sqlite3.Connection | None = None) -> None:
        if db is None:
            with self.connect() as connection:
                self.save_copy(value, db=connection)
            return
        prior = db.execute(
            "SELECT * FROM clip_copies WHERE event_id=? AND revision=?",
            (value["event_id"], value["revision"]),
        ).fetchone()
        keys = (
            "event_id",
            "clip_id",
            "revision",
            "relative_path",
            "size_bytes",
            "checksum",
            "created_at",
        )
        if prior:
            if any(prior[key] != value[key] for key in keys):
                raise ValueError("copy metadata is immutable")
            return
        db.execute(
            "INSERT INTO clip_copies(event_id,clip_id,revision,relative_path,"
            "size_bytes,checksum,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            tuple(value[key] for key in keys),
        )

    def copies(self, ready_only: bool = False, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            condition = (
                (
                    " AND EXISTS(SELECT 1 FROM outbox o WHERE o.event_id=c.event_id "
                    "AND o.revision=c.revision AND o.state='ACKED')"
                )
                if ready_only
                else ""
            )
            return [
                dict(row)
                for row in db.execute(
                    "SELECT c.* FROM clip_copies c WHERE c.state NOT IN ('DELETED','QUARANTINED') "
                    "AND c.next_at<=?" + condition + " ORDER BY c.next_at,c.created_at LIMIT ?",
                    (self.clock(), limit),
                )
            ]

    def copy(self, event_id: str, revision: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM clip_copies WHERE event_id=? AND revision=?", (event_id, revision)
            ).fetchone()
            return dict(row) if row else None

    def copy_state(
        self, event_id: str, revision: int, state: str, delay_ms=0, receipt: dict | None = None
    ) -> None:
        if state not in {"LOCAL", "STORED", "RETIRING", "DELETED", "QUARANTINED"}:
            raise ValueError("invalid copy state")
        with self.connect() as db:
            db.execute(
                "UPDATE clip_copies SET state=?,next_at=?,attempts=attempts+1,"
                "receipt=COALESCE(?,receipt) "
                "WHERE event_id=? AND revision=?",
                (
                    state,
                    self.clock() + delay_ms,
                    canonical(receipt) if receipt else None,
                    event_id,
                    revision,
                ),
            )

    def copy_rows(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM clip_copies ORDER BY created_at,event_id,revision"
                )
            ]

    def retry_copy(self, event_id: str, revision: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE clip_copies SET state='LOCAL',next_at=0,attempts=0 "
                "WHERE event_id=? AND revision=? AND state='QUARANTINED'",
                (event_id, revision),
            )

    def unstaged(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT o.event_id,o.revision,o.kind,o.body,m.relative_path FROM outbox o "
                    "JOIN manifests m ON m.event_id=o.event_id "
                    "WHERE NOT EXISTS(SELECT 1 FROM clip_copies c WHERE c.event_id=o.event_id "
                    "AND c.revision=o.revision) "
                    "AND NOT EXISTS(SELECT 1 FROM copy_recovery f WHERE f.event_id=o.event_id "
                    "AND f.revision=o.revision AND f.next_at>?) "
                    "AND ((o.kind='event' AND json_extract(o.body,'$.media.status')='AVAILABLE') "
                    "OR (o.kind='media' AND json_extract(o.body,'$.status')='AVAILABLE')) "
                    "ORDER BY o.id LIMIT 10",
                    (self.clock(),),
                )
            ]

    def defer_recovery(self, event_id: str, revision: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO copy_recovery VALUES(?,?,?) ON CONFLICT(event_id,revision) "
                "DO UPDATE SET next_at=excluded.next_at",
                (event_id, revision, self.clock() + 60000),
            )
