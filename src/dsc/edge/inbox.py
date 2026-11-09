import json
import logging
import os
import stat

from pydantic import ValidationError

from dsc.edge.config import EdgeSettings
from dsc.edge.paths import check_path
from dsc.edge.store import EdgeStore
from dsc.shared.contracts import EventEnvelope, MediaEnvelope
from dsc.shared.observability import emit_event, logged


@logged(
    level=logging.DEBUG, fields={"edgeServerId": "settings.server_id", "siteId": "settings.site_id"}
)
def receive_inbox(settings: EdgeSettings, store: EdgeStore) -> int:
    inbox = settings.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    for name in ("accepted", "rejected"):
        (inbox / name).mkdir(exist_ok=True)
    accepted = 0
    for path in sorted(inbox.glob("*.json")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > settings.max_inbox_bytes:
                    raise ValueError("invalid inbox file")
                data = stream.read(settings.max_inbox_bytes + 1)
                if len(data) > settings.max_inbox_bytes:
                    raise ValueError("inbox file exceeds maximum size")
                raw = json.loads(data)
                if not isinstance(raw, dict):
                    raise ValueError("inbox body must be an object")
            cls = EventEnvelope if raw.get("kind") == "event" else MediaEnvelope
            envelope = cls.model_validate(raw)
            if isinstance(envelope, EventEnvelope):
                expected = f"{envelope.event.event_id}.event.json"
                if envelope.event.site_id != settings.site_id:
                    raise ValueError("site mismatch")
            else:
                expected = f"{envelope.event_id}.{envelope.media.revision}.media.json"
            if path.name != expected:
                raise ValueError("filename does not match body identity")
            if envelope.local_media:
                check_path(settings.nas_root, envelope.local_media.relative_path)
            store.accept(envelope)
            # A crash here leaves an idempotent receipt for the remaining input file.
            os.replace(path, inbox / "accepted" / path.name)
            accepted += 1
        except (ValueError, ValidationError, OSError) as exc:
            # Storage faults leave the input in place for a retry.
            if isinstance(exc, OSError) and exc.errno not in {40, 62}:
                raise
            reason = {"code": "INVALID_LOCAL_INPUT", "type": type(exc).__name__}
            os.replace(path, inbox / "rejected" / path.name)
            (inbox / "rejected" / f"{path.name}.reason").write_text(json.dumps(reason) + "\n")
            emit_event(
                "inbox_rejected",
                level=logging.WARNING,
                errorCode=reason["code"],
                errorType=reason["type"],
            )
    if accepted:
        emit_event("inbox_accepted", acceptedCount=accepted)
    return accepted
