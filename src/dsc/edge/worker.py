from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import threading
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from dsc.edge.client import EdgeClient, response_code
from dsc.edge.config import EdgeSettings
from dsc.edge.paths import open_clip
from dsc.edge.store import EdgeStore
from dsc.shared.observability import contextual, emit_event, logged


def retry_delay(attempts: int, retry_after: str | None = None) -> int:
    if retry_after:
        try:
            if retry_after.isdigit():
                return max(0, int(retry_after)) * 1000
            return (
                max(
                    0, int((parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds())
                )
                * 1000
            )
        except (ValueError, TypeError, OverflowError):
            pass
    upper = min(60, 2 ** min(attempts, 6))
    return int(random.uniform(upper / 2, upper) * 1000)


@logged(
    level=logging.DEBUG, fields={"edgeServerId": "settings.server_id", "siteId": "settings.site_id"}
)
def send_pending(client: EdgeClient, store: EdgeStore, settings: EdgeSettings) -> bool:
    item = store.next_item()
    if item is None:
        return False
    return send_item(client, store, settings, item)


@logged(fields={"eventId": "item.event_id", "outboxId": "item.id"})
def send_item(client: EdgeClient, store: EdgeStore, settings: EdgeSettings, item: dict) -> bool:
    method, path = (
        ("POST", "/ingest")
        if item["kind"] == "event"
        else ("PUT", f"/ingest/events/{item['event_id']}/media")
    )
    try:
        response = client.request(method, path, json.loads(item["body"]))
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        store.mark(item["id"], "PENDING", retry_delay(item["attempts"]), "CONNECTION_OR_AUTH_ERROR")
        return True
    status = response.status_code
    if status in {200, 201}:
        try:
            value = response.json()
            if value["eventId"] != item["event_id"]:
                raise ValueError()
            if item["kind"] == "event":
                if (
                    value["edgeServerId"] != settings.server_id
                    or value["siteId"] != settings.site_id
                    or not isinstance(value["receivedAt"], str)
                    or value["duplicate"] is not (status == 200)
                ):
                    raise ValueError()
            elif status != 200 or value["revision"] != item["revision"]:
                raise ValueError()
            store.mark(item["id"], "ACKED")
        except (ValueError, KeyError, TypeError):
            store.mark(item["id"], "PENDING", retry_delay(item["attempts"]), "INVALID_ACK")
    elif status == 401 or status == 429 or status >= 500:
        store.mark(
            item["id"],
            "PENDING",
            retry_delay(item["attempts"], response.headers.get("Retry-After")),
            response_code(response),
        )
    else:
        store.mark(item["id"], "QUARANTINED", code=response_code(response))
    return True


@logged(
    fields={
        "edgeServerId": "settings.server_id",
        "siteId": "settings.site_id",
        "eventId": "job.eventId",
        "requestId": "job.requestId",
        "clipId": "job.clipId",
    }
)
def run_job(client: EdgeClient, store: EdgeStore, settings: EdgeSettings, job: dict) -> None:
    path = f"/edge-server/jobs/{job['requestId']}"
    token = {"leaseToken": job["leaseToken"]}
    stop, lost = threading.Event(), threading.Event()

    def renew():
        while not stop.wait(settings.lease_renew_seconds):
            try:
                client.post(f"{path}/lease", token)
            except (httpx.HTTPError, ValueError, OSError):
                emit_event("job_lease_lost", level=logging.WARNING)
                lost.set()
                return

    thread = threading.Thread(target=contextual(renew), daemon=True)
    thread.start()
    failure = None
    try:
        manifest = store.manifest(job["clipId"], job["eventId"])
        if manifest is None:
            failure = "SOURCE_NOT_FOUND"
            return
        try:
            with open_clip(settings.nas_root, manifest["relative_path"]) as stream:
                before = os.fstat(stream.fileno())
                if not 0 < before.st_size <= settings.max_clip_bytes:
                    failure = "UPLOAD_VERIFICATION_FAILED"
                    return
                checksum = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    if lost.is_set():
                        return
                    checksum.update(chunk)
                encoded = base64.b64encode(checksum.digest()).decode()
                upload = client.post(
                    f"{path}/upload-url",
                    {
                        **token,
                        "sizeBytes": before.st_size,
                        "checksumSha256": encoded,
                        "contentType": "video/mp4",
                    },
                )
                stream.seek(0)
                client.put_clip(upload["uploadUrl"], upload["headers"], stream, before.st_size)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    failure = "UPLOAD_VERIFICATION_FAILED"
                    return
                if not lost.is_set():
                    client.post(f"{path}/complete", token)
        except FileNotFoundError:
            failure = "SOURCE_NOT_FOUND"
        except (PermissionError, OSError):
            failure = "SOURCE_UNREACHABLE"
        except ValueError:
            failure = "SOURCE_NOT_FOUND"
        except httpx.HTTPStatusError as exc:
            failure = (
                "UPLOAD_VERIFICATION_FAILED"
                if response_code(exc.response) == "UPLOAD_VERIFICATION_FAILED"
                else "UPLOAD_FAILED"
            )
        except httpx.RequestError:
            failure = "UPLOAD_FAILED"
    finally:
        stop.set()
        thread.join(timeout=settings.request_timeout + 1)
        if failure and not lost.is_set():
            emit_event("clip_job_failed", level=logging.WARNING, errorCode=failure)
            try:
                client.post(
                    f"{path}/fail",
                    {**token, "failureCode": failure, "retryable": failure != "SOURCE_NOT_FOUND"},
                )
            except (httpx.HTTPError, ValueError, OSError):
                emit_event("job_failure_report_pending", level=logging.WARNING)
