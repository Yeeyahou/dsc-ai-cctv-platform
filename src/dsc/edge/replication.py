"""Retryable NAS replication and conservative expiry of managed field copies."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx

from dsc.edge.client import EdgeClient, response_code
from dsc.edge.config import EdgeSettings
from dsc.edge.files import copy_lock, delete_copy, fingerprint, stage_clip
from dsc.edge.paths import open_clip
from dsc.edge.store import EdgeStore
from dsc.edge.worker import retry_delay
from dsc.shared.contracts import EventInput, MediaUpdate
from dsc.shared.observability import emit_event, logged


def copy_body(copy: dict) -> dict:
    return {
        "eventId": copy["event_id"],
        "clipId": copy["clip_id"],
        "revision": copy["revision"],
        "sizeBytes": copy["size_bytes"],
        "checksumSha256": copy["checksum"],
    }


def valid_receipt(receipt: dict, body: dict, transfer: dict, source_id: str) -> None:
    expected = {
        **body,
        "transferId": transfer["transferId"],
        "sourceId": source_id,
        "receiverId": transfer["receiverId"],
        "status": "STORED",
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("office acknowledgement does not identify this exact clip")


@logged(fields={"eventId": "copy.event_id", "clipId": "copy.clip_id"})
def synchronize_copy(
    client: EdgeClient, store: EdgeStore, settings: EdgeSettings, copy: dict
) -> None:
    body = copy_body(copy)
    with copy_lock(settings.state_dir, copy["event_id"], copy["revision"], exclusive=True):
        if copy["state"] == "RETIRING":
            finish_retirement(client, store, settings, copy, body)
            return
        with open_clip(settings.cache_root, copy["relative_path"]) as stream:
            if fingerprint(stream) != (copy["size_bytes"], copy["checksum"]):
                raise ValueError("managed field copy has changed")
            try:
                registered = client.post("/edge-server/replicas", body)
            except httpx.HTTPStatusError as exc:
                if response_code(exc.response) != "REPLICA_RETIRED":
                    raise
                store.copy_state(copy["event_id"], copy["revision"], "RETIRING")
                finish_retirement(client, store, settings, copy, body)
                return
            if any(
                registered.get(key) != value for key, value in {**body, "status": "PRESENT"}.items()
            ):
                raise ValueError("backend copy acknowledgement mismatch")
            transfer = client.post("/edge-server/archive/prepare", body)
            url = urlsplit(transfer["uploadUrl"])
            if (
                url.scheme != "https"
                or url.netloc not in settings.archive_allowed_hosts
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not url.path.endswith(f"/archive/transfers/{transfer['transferId']}")
            ):
                raise ValueError("unregistered archive receiver endpoint")
            headers = {"Authorization": f"Bearer {transfer['transferToken']}"}
            response = client.http.post(
                f"{transfer['uploadUrl']}/verify", headers=headers, timeout=settings.upload_timeout
            )
            if response.status_code == 404:
                stream.seek(0)
                response = client.http.put(
                    transfer["uploadUrl"],
                    content=stream,
                    headers={
                        **headers,
                        "Content-Type": "video/mp4",
                        "Content-Length": str(copy["size_bytes"]),
                    },
                    timeout=settings.upload_timeout,
                )
            response.raise_for_status()
            receipt = response.json()
            valid_receipt(receipt, body, transfer, settings.server_id)
        due = copy["created_at"] + settings.retention_days * 86400000
        store.copy_state(
            copy["event_id"],
            copy["revision"],
            "STORED",
            max(settings.replication_seconds * 1000, due - store.clock()),
            receipt,
        )
        if due <= store.clock():
            # Server retirement fences new claims before the file is removed locally.
            retired = client.post("/edge-server/replicas/retire", body)
            if any(
                retired.get(key) != value for key, value in {**body, "status": "RETIRED"}.items()
            ):
                raise ValueError("backend retirement acknowledgement mismatch")
            store.copy_state(copy["event_id"], copy["revision"], "RETIRING")
            finish_retirement(client, store, settings, copy, body)


def finish_retirement(
    client: EdgeClient, store: EdgeStore, settings: EdgeSettings, copy: dict, body: dict
) -> None:
    retired = client.post("/edge-server/replicas/retire", body)
    if any(retired.get(key) != value for key, value in {**body, "status": "RETIRED"}.items()):
        raise ValueError("backend retirement acknowledgement mismatch")
    try:
        delete_copy(
            settings.cache_root, copy["relative_path"], copy["size_bytes"], copy["checksum"]
        )
    except FileNotFoundError:
        pass
    store.copy_state(copy["event_id"], copy["revision"], "DELETED")
    emit_event("field_copy_retired", eventId=copy["event_id"], clipId=copy["clip_id"])


@logged(level=logging.DEBUG)
def replicate_pending(client: EdgeClient, store: EdgeStore, settings: EdgeSettings) -> None:
    for copy in store.copies(ready_only=True, limit=10):
        if copy["state"] == "QUARANTINED":
            continue
        try:
            synchronize_copy(client, store, settings, copy)
        except (ValueError, KeyError, TypeError):
            store.copy_state(copy["event_id"], copy["revision"], "QUARANTINED")
            emit_event(
                "clip_copy_quarantined",
                level=logging.ERROR,
                eventId=copy["event_id"],
                clipId=copy["clip_id"],
                errorCode="COPY_INTEGRITY_CONFLICT",
            )
        except httpx.HTTPStatusError as exc:
            code = response_code(exc.response)
            transient = code in {"ARCHIVE_NOT_CONFIGURED", "REPLICA_IN_USE", "TRANSFER_EXPIRED"}
            status = exc.response.status_code
            terminal = 400 <= status < 500 and status not in {401, 429} and not transient
            if terminal or (status == 401 and copy["attempts"] >= 2):
                store.copy_state(copy["event_id"], copy["revision"], "QUARANTINED")
                emit_event(
                    "clip_copy_quarantined",
                    level=logging.ERROR,
                    eventId=copy["event_id"],
                    errorCode=code,
                    status=status,
                )
            else:
                current = store.copy(copy["event_id"], copy["revision"])
                store.copy_state(
                    copy["event_id"],
                    copy["revision"],
                    current["state"],
                    retry_delay(copy["attempts"], exc.response.headers.get("Retry-After")),
                )
                emit_event(
                    "archive_replication_pending",
                    level=logging.WARNING,
                    eventId=copy["event_id"],
                    errorCode=code,
                    status=status,
                )
        except (httpx.HTTPError, OSError):
            current = store.copy(copy["event_id"], copy["revision"])
            store.copy_state(
                copy["event_id"], copy["revision"], current["state"], retry_delay(copy["attempts"])
            )
            emit_event(
                "archive_replication_pending",
                level=logging.WARNING,
                eventId=copy["event_id"],
                clipId=copy["clip_id"],
            )


@logged(level=logging.DEBUG)
def recover_unstaged(store: EdgeStore, settings: EdgeSettings) -> None:
    for row in store.unstaged():
        try:
            body = json.loads(row["body"])
            media = (
                EventInput.model_validate(body).media
                if row["kind"] == "event"
                else MediaUpdate.model_validate(body)
            )
            staged = stage_clip(settings, row["event_id"], media, row["relative_path"])
            store.save_copy(staged)
        except (ValueError, OSError):
            store.defer_recovery(row["event_id"], row["revision"])
            emit_event(
                "local_copy_recovery_pending",
                level=logging.ERROR,
                eventId=row["event_id"],
                revision=row["revision"],
            )
