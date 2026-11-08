from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from dsc.backend.auth import Access
from dsc.backend.config import Settings
from dsc.backend.db import (
    Agent,
    Asset,
    Attempt,
    Audit,
    Event,
    EventAsset,
    EventMedia,
    FileJob,
    Principal,
)
from dsc.backend.errors import Problem, missing
from dsc.shared.contracts import (
    EventInput,
    FailureBody,
    Heartbeat,
    MediaUpdate,
    UploadBody,
    canonical,
    digest,
    iso,
    milliseconds,
    now_ms,
)
from dsc.shared.observability import emit_event, log_methods, logged


@dataclass(frozen=True)
class Filters:
    start: int
    end: int
    site_id: str | None = None
    asset_id: str | None = None
    event_code: str | None = None
    limit: int = 100

    def validate(self) -> None:
        if not self.site_id and not self.asset_id:
            raise Problem(422, "INVALID_FILTER", "siteId 또는 assetId가 필요합니다")
        if self.end <= self.start or self.end - self.start > 31 * 86400000:
            raise Problem(422, "INVALID_PERIOD", "조회 기간은 0일 초과 31일 이하여야 합니다")
        if not 1 <= self.limit <= 500:
            raise Problem(422, "INVALID_LIMIT", "limit는 1~500이어야 합니다")


@log_methods(
    fields={
        "principal": "access.principal.id",
        "agentId": "access.agent.id",
        "siteId": "access.agent.site_id",
    }
)
class Platform:
    def __init__(self, sessions, settings: Settings, storage, queue, clock=now_ms):
        self.sessions = sessions
        self.settings = settings
        self.storage = storage
        self.queue = queue
        self.clock = clock

    def _agent(self, access: Access) -> Agent:
        access.require("dsc/agent.work")
        if access.agent is None:
            raise Problem(403, "AGENT_REQUIRED", "등록된 에이전트가 필요합니다")
        return access.agent

    @logged(
        fields={
            "eventId": "body.event_id",
            "siteId": "body.site_id",
            "principal": "access.principal.id",
            "agentId": "access.agent.id",
        }
    )
    def ingest(self, access: Access, body: EventInput) -> tuple[dict, int]:
        access.require("dsc/ingest.write")
        agent = access.agent
        if agent is None or body.site_id != agent.site_id:
            raise Problem(403, "SITE_MISMATCH", "등록 센터와 일치하지 않습니다")
        payload = body.wire()
        event_id = str(body.event_id)
        try:
            with self.sessions.begin() as session:
                prior = session.get(Event, event_id)
                if prior:
                    return self._duplicate(prior, agent.id, payload)
                for link in body.assets:
                    asset = session.get(Asset, link.asset_id)
                    if (
                        asset is None
                        or asset.site_id != agent.site_id
                        or asset.id not in agent.assets
                    ):
                        raise Problem(403, "ASSET_MISMATCH", "허용되지 않은 자산입니다")
                    for key in {"fireAnomalyScore", "noiseVariance"} & (body.metrics or {}).keys():
                        rule = (
                            asset.aas.get("metricRanges", {})
                            .get(body.model_version or "", {})
                            .get(key)
                        )
                        if rule is not None:
                            value = body.metrics[key]
                            if not rule["min"] <= value <= rule["max"]:
                                raise Problem(
                                    422, "METRIC_RANGE", "등록된 수치 범위를 벗어났습니다"
                                )
                for key in {"fireAnomalyScore", "noiseVariance"} & (body.metrics or {}).keys():
                    rules = [
                        session.get(Asset, a.asset_id)
                        .aas.get("metricRanges", {})
                        .get(body.model_version or "", {})
                        .get(key)
                        for a in body.assets
                    ]
                    if not any(rules):
                        raise Problem(
                            422, "METRIC_METADATA_REQUIRED", "모델별 수치 범위 등록이 필요합니다"
                        )
                row = Event(
                    id=event_id,
                    agent_id=agent.id,
                    site_id=body.site_id,
                    event_code=body.event_code,
                    occurred_at=milliseconds(body.occurred_at),
                    received_at=self.clock(),
                    incident_id=str(body.incident_id) if body.incident_id else None,
                    payload=payload,
                    payload_hash=digest(payload),
                )
                session.add(row)
                session.flush()
                session.add_all(
                    EventAsset(event_id=event_id, asset_id=x.asset_id, role=x.role)
                    for x in body.assets
                )
                if body.media:
                    session.add(
                        EventMedia(
                            event_id=event_id,
                            clip_id=str(body.media.clip_id),
                            revision=0,
                            payload=body.media.wire(),
                        )
                    )
                result = self._ack(row, False)
            return result, 201
        except IntegrityError:
            with self.sessions() as session:
                prior = session.get(Event, event_id)
                if prior:
                    return self._duplicate(prior, agent.id, payload)
            raise

    def _duplicate(self, prior: Event, agent_id: str, payload: dict) -> tuple[dict, int]:
        if prior.agent_id != agent_id or prior.payload_hash != digest(payload):
            raise Problem(
                409, "EVENT_ID_CONFLICT", "동일 eventId에 다른 이벤트가 저장되어 있습니다"
            )
        return self._ack(prior, True), 200

    @staticmethod
    def _ack(row: Event, duplicate: bool) -> dict:
        return {
            "eventId": row.id,
            "agentId": row.agent_id,
            "siteId": row.site_id,
            "receivedAt": iso(row.received_at),
            "duplicate": duplicate,
        }

    def media(self, access: Access, event_id: str, body: MediaUpdate) -> dict:
        access.require("dsc/ingest.write")
        with self.sessions.begin() as session:
            row = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
            if row is None or access.agent is None or row.agent_id != access.agent.id:
                raise missing()
            media = session.get(EventMedia, event_id)
            if media:
                if media.clip_id != str(body.clip_id):
                    raise Problem(409, "CLIP_ID_CONFLICT", "clipId는 교체할 수 없습니다")
                if media.revision == body.revision and media.payload == body.wire():
                    return {"eventId": event_id, **media.payload}
                if body.revision <= media.revision:
                    raise Problem(
                        409, "MEDIA_REVISION_CONFLICT", "오래되었거나 충돌하는 revision입니다"
                    )
                media.revision, media.payload = body.revision, body.wire()
            else:
                session.add(
                    EventMedia(
                        event_id=event_id,
                        clip_id=str(body.clip_id),
                        revision=body.revision,
                        payload=body.wire(),
                    )
                )
        return {"eventId": event_id, **body.wire()}

    def heartbeat(self, access: Access, body: Heartbeat) -> dict:
        agent = self._agent(access)
        with self.sessions.begin() as session:
            row = session.get(Agent, agent.id)
            row.last_seen, row.heartbeat = self.clock(), body.wire()
        emit_event(
            "agent_heartbeat",
            agentId=agent.id,
            siteId=agent.site_id,
            receivedAt=iso(self.clock()),
            outboxPendingCount=body.outbox_pending_count,
            oldestPendingAt=body.wire().get("oldestPendingAt"),
            diskFreeBytes=body.disk_free_bytes,
            nasStatus=body.nas_status,
        )
        return {"agentId": agent.id, "serverTime": iso(self.clock())}

    @staticmethod
    def _view(session, row: Event) -> dict:
        result = {**row.payload, "agentId": row.agent_id, "receivedAt": iso(row.received_at)}
        media = session.get(EventMedia, row.id)
        if media:
            result["media"] = {**media.payload, "revision": media.revision}
        return result

    def get_event(self, access: Access, event_id: str) -> dict:
        access.require("dsc/timeseries.read")
        with self.sessions.begin() as session:
            row = session.get(Event, event_id)
            if row is None:
                raise missing()
            access.check_event(session, row)
            self._audit(session, access, "event.read", row.id)
            return self._view(session, row)

    def _query(self, access: Access, filters: Filters):
        query = select(Event).where(
            Event.occurred_at >= filters.start,
            Event.occurred_at < filters.end,
            access.event_clause(),
        )
        if filters.site_id:
            query = query.where(Event.site_id == filters.site_id)
        if filters.asset_id:
            query = query.where(
                Event.id.in_(
                    select(EventAsset.event_id).where(EventAsset.asset_id == filters.asset_id)
                )
            )
        if filters.event_code:
            query = query.where(Event.event_code == filters.event_code)
        return query

    def events(self, access: Access, filters: Filters, cursor: str | None = None) -> dict:
        access.require("dsc/timeseries.read")
        filters.validate()
        context = digest(
            {
                "filters": vars(filters),
                "principal": access.principal.id,
                "sites": sorted(access.principal.sites),
                "assets": sorted(access.principal.assets),
                "scopes": sorted(access.scopes),
            }
        )
        query = self._query(access, filters)
        if cursor:
            last = self._decode_cursor(cursor, context)
            query = query.where(
                or_(
                    Event.occurred_at < last[0],
                    and_(Event.occurred_at == last[0], Event.id < last[1]),
                )
            )
        with self.sessions.begin() as session:
            rows = list(
                session.scalars(
                    query.order_by(Event.occurred_at.desc(), Event.id.desc()).limit(
                        filters.limit + 1
                    )
                )
            )
            result = {"items": [self._view(session, r) for r in rows[: filters.limit]]}
            if len(rows) > filters.limit:
                last = rows[filters.limit - 1]
                result["nextCursor"] = self._encode_cursor(context, [last.occurred_at, last.id])
            self._audit(session, access, "events.list", filters.asset_id or filters.site_id or "")
            return result

    def current(self, access: Access, asset_id: str) -> dict:
        access.require("dsc/timeseries.read")
        with self.sessions() as session:
            asset = session.get(Asset, asset_id)
            if asset is None or not access.asset_allowed(asset):
                raise missing()
            now = self.clock()
            start = milliseconds(
                datetime.fromtimestamp(now / 1000, UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            )
            query = self._query(access, Filters(start, now + 1, asset_id=asset_id)).subquery()
            count, low, high = session.execute(
                select(func.count(), func.min(query.c.occurred_at), func.max(query.c.occurred_at))
            ).one()
            result = {"assetId": asset_id, "recordCount": count, "asOf": iso(now)}
            if count:
                result.update(startTime=iso(low), endTime=iso(high))
            return result

    def _encode_cursor(self, context: str, last: list) -> str:
        raw = canonical({"context": context, "last": last, "expires": self.clock() + 3600000})
        encoded = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        signature = hmac.new(
            self.settings.cursor_secret.get_secret_value().encode(),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{encoded}.{signature}"

    def _decode_cursor(self, cursor: str, context: str) -> list:
        try:
            if len(cursor) > 4096:
                raise ValueError()
            encoded, signature = cursor.split(".")
            expected = hmac.new(
                self.settings.cursor_secret.get_secret_value().encode(),
                encoded.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if value["context"] != context or value["expires"] < self.clock():
                raise ValueError()
            return value["last"]
        except (ValueError, KeyError, TypeError) as exc:
            raise Problem(
                422, "INVALID_CURSOR", "유효하지 않거나 권한이 변경된 cursor입니다"
            ) from exc

    def _audit(self, session, access: Access, action: str, object_id: str) -> None:
        session.add(
            Audit(
                principal_id=access.principal.id,
                action=action,
                object_id=object_id,
                at=self.clock(),
            )
        )

    def request_clip(self, access: Access, event_id: str) -> tuple[dict, int]:
        access.require("dsc/timeseries.read")
        with self.sessions.begin() as session:
            event = session.scalar(select(Event).where(Event.id == event_id).with_for_update())
            if event is None:
                raise missing()
            access.check_event(session, event, video=True)
            media = session.get(EventMedia, event_id)
            if media is None or media.payload["status"] in {"MISSING", "NOT_APPLICABLE"}:
                raise missing("CLIP_UNAVAILABLE")
            if media.payload["status"] != "AVAILABLE":
                raise Problem(409, "CLIP_NOT_READY", "클립 생성이 완료되지 않았습니다")
            key = f"{event.id}:{media.clip_id}"
            prior = session.scalar(
                select(FileJob).where(FileJob.active_key == key).with_for_update()
            )
            if prior:
                self._expire(session, prior)
            if prior and prior.active_key:
                job = prior
            else:
                job = FileJob(
                    id=str(uuid4()),
                    event_id=event_id,
                    clip_id=media.clip_id,
                    agent_id=event.agent_id,
                    requested_by=access.principal.id,
                    active_key=key,
                    status="QUEUED",
                    created_at=self.clock(),
                    deadline=self.clock() + self.settings.job_seconds * 1000,
                    attempt=0,
                    dispatch_version=1,
                    dispatched_version=0,
                    retryable=False,
                )
                session.add(job)
                session.flush()
            self._audit(session, access, "clip.request", job.id)
            result, status = self._job_view(session, access, job)
        self._try_dispatch(job.id)
        return result, status

    def file_status(
        self, access: Access, object_id: str, by_event: bool = False
    ) -> tuple[dict, int]:
        access.require("dsc/timeseries.read")
        with self.sessions.begin() as session:
            query = (
                select(FileJob).where(FileJob.event_id == object_id)
                if by_event
                else (select(FileJob).where(FileJob.id == object_id))
            )
            job = session.scalar(
                query.order_by(FileJob.created_at.desc()).limit(1).with_for_update()
            )
            if job is None:
                raise missing("FILE_REQUEST_NOT_FOUND")
            access.check_event(session, session.get(Event, job.event_id), video=True)
            self._expire(session, job)
            self._audit(session, access, "clip.status", job.id)
            return self._job_view(session, access, job)

    def _job_view(self, session, access: Access, job: FileJob) -> tuple[dict, int]:
        result = {
            "requestId": job.id,
            "eventId": job.event_id,
            "clipId": job.clip_id,
            "status": job.status,
            "statusUrl": f"/file-requests/{job.id}",
        }
        if job.status in {"QUEUED", "UPLOADING"}:
            result["retryAfterSec"] = 5
            return result, 202
        if job.status == "READY":
            attempt = self._attempt(session, job)
            seconds = min(
                self.settings.get_url_seconds, (job.object_expires_at - self.clock()) // 1000
            )
            if seconds < 1:
                self._terminal(job, "EXPIRED")
                result["status"] = "EXPIRED"
                return result, 200
            result.update(
                downloadUrl=self.storage.download_url(attempt.object_key, seconds),
                urlExpiresAt=iso(self.clock() + seconds * 1000),
                objectExpiresAt=iso(job.object_expires_at),
                sizeBytes=attempt.size_bytes,
                contentType="video/mp4",
            )
            self._audit(session, access, "clip.url", job.id)
        elif job.status == "FAILED":
            result.update(failureCode=job.failure_code, retryable=job.retryable)
        return result, 200

    @staticmethod
    def _attempt(session, job: FileJob) -> Attempt | None:
        return session.scalar(
            select(Attempt).where(Attempt.request_id == job.id, Attempt.number == job.attempt)
        )

    def _terminal(self, job: FileJob, status: str, code: str | None = None) -> None:
        job.status, job.active_key, job.failure_code = status, None, code

    def _expire(self, session, job: FileJob) -> None:
        now = self.clock()
        if job.status == "READY" and job.object_expires_at <= now:
            self._terminal(job, "EXPIRED")
        elif job.status in {"QUEUED", "UPLOADING"} and job.deadline <= now:
            self._terminal(job, "FAILED", "JOB_TIMEOUT")
            job.retryable = True
        elif job.status == "UPLOADING" and self._attempt(session, job).lease_until <= now:
            self._retry(job, "LEASE_EXPIRED", True)

    def _retry(self, job: FileJob, code: str, retryable: bool) -> None:
        job.failure_code, job.retryable = code, retryable
        if retryable and job.attempt < self.settings.max_attempts and job.deadline > self.clock():
            job.status = "QUEUED"
            job.dispatch_version += 1
        else:
            self._terminal(job, "FAILED", code)

    def _try_dispatch(self, job_id: str) -> None:
        try:
            with self.sessions.begin() as session:
                job = session.scalar(select(FileJob).where(FileJob.id == job_id).with_for_update())
                if job and job.status == "QUEUED" and job.dispatched_version < job.dispatch_version:
                    agent = session.get(Agent, job.agent_id)
                    self.queue.send(agent.queue_url, job.id, job.dispatch_version)
                    # A crash before this marker commits causes a fenced duplicate delivery.
                    job.dispatched_version = job.dispatch_version
        except Exception:
            # DB already accepted the request; maintenance recovers this explicit delivery gap.
            emit_event("queue_dispatch_pending", level=logging.WARNING, requestId=job_id)

    def claim(self, access: Access) -> dict | None:
        agent = self._agent(access)

        def messages():
            for _ in range(10):
                batch = self.queue.receive(agent.queue_url)
                if not batch:
                    return
                yield from batch

        for message in messages():
            receipt = message["ReceiptHandle"]
            try:
                data = json.loads(message["Body"])
                job_id, version = data["requestId"], data["dispatchVersion"]
            except (ValueError, KeyError, TypeError):
                self.queue.delete(agent.queue_url, receipt)
                continue
            with self.sessions.begin() as session:
                job = session.scalar(select(FileJob).where(FileJob.id == job_id).with_for_update())
                if job is None or job.agent_id != agent.id:
                    self.queue.delete(agent.queue_url, receipt)
                    continue
                self._expire(session, job)
                if job.status != "QUEUED" or version != job.dispatch_version:
                    if job.status != "UPLOADING" or version != job.dispatch_version:
                        self.queue.delete(agent.queue_url, receipt)
                    continue
                if not self._requester_allowed(session, job):
                    self._terminal(job, "FAILED", "REQUESTER_DISABLED")
                    self.queue.delete(agent.queue_url, receipt)
                    continue
                event = session.get(Event, job.event_id)
                if event.agent_id != agent.id or event.site_id != agent.site_id:
                    raise missing()
                job.attempt += 1
                job.status = "UPLOADING"
                token = secrets.token_urlsafe(32)
                until = min(job.deadline, self.clock() + self.settings.lease_seconds * 1000)
                session.add(
                    Attempt(
                        request_id=job.id,
                        number=job.attempt,
                        token_hash=digest(token),
                        lease_until=until,
                        receipt=receipt,
                        object_key=f"clips/{job.id}/{job.attempt}/{uuid4()}.mp4",
                    )
                )
                result = {
                    "requestId": job.id,
                    "eventId": job.event_id,
                    "clipId": job.clip_id,
                    "attempt": job.attempt,
                    "leaseToken": token,
                    "leaseExpiresAt": iso(until),
                }
            return result
        return None

    def _requester_allowed(self, session, job: FileJob) -> bool:
        requester = session.get(Principal, job.requested_by)
        if requester is None or not requester.active:
            return False
        try:
            actor = Access(requester, frozenset(requester.scopes), None)
            actor.require("dsc/timeseries.read")
            actor.check_event(session, session.get(Event, job.event_id), video=True)
        except Problem:
            return False
        return True

    def _leased(self, session, access: Access, job_id: str, token: str, completed=False):
        agent = self._agent(access)
        job = session.scalar(select(FileJob).where(FileJob.id == job_id).with_for_update())
        if job is None or job.agent_id != agent.id:
            raise missing()
        attempt = self._attempt(session, job)
        if attempt is None or not hmac.compare_digest(attempt.token_hash, digest(token)):
            raise Problem(409, "LEASE_CONFLICT", "유효한 현재 시도의 lease가 필요합니다")
        if completed and job.status == "READY":
            return job, attempt
        if not self._requester_allowed(session, job):
            raise Problem(409, "REQUESTER_DISABLED", "요청자의 읽기 권한이 회수되었습니다")
        if job.status != "UPLOADING" or min(attempt.lease_until, job.deadline) <= self.clock():
            raise Problem(409, "LEASE_EXPIRED", "작업 lease가 만료되었습니다")
        return job, attempt

    def renew(self, access: Access, job_id: str, token: str) -> dict:
        with self.sessions.begin() as session:
            job, attempt = self._leased(session, access, job_id, token)
            until = min(job.deadline, self.clock() + self.settings.lease_seconds * 1000)
            self.queue.extend(
                access.agent.queue_url, attempt.receipt, max(1, (until - self.clock()) // 1000)
            )
            attempt.lease_until = until
            return {"leaseExpiresAt": iso(until)}

    def upload_url(self, access: Access, job_id: str, body: UploadBody) -> dict:
        with self.sessions.begin() as session:
            job, attempt = self._leased(session, access, job_id, body.lease_token)
            media = session.get(EventMedia, job.event_id)
            if media is None or media.payload["status"] != "AVAILABLE":
                raise missing("CLIP_UNAVAILABLE")
            if body.size_bytes != media.payload["sizeBytes"]:
                raise Problem(409, "UPLOAD_SIZE_MISMATCH", "등록된 클립 크기와 다릅니다")
            if attempt.checksum and (
                attempt.checksum != body.checksum_sha256 or attempt.size_bytes != body.size_bytes
            ):
                raise Problem(409, "UPLOAD_METADATA_CONFLICT", "같은 시도의 업로드 값은 불변입니다")
            attempt.size_bytes, attempt.checksum = body.size_bytes, body.checksum_sha256
            seconds = min(
                self.settings.put_url_seconds, max(1, (job.deadline - self.clock()) // 1000)
            )
            url, headers = self.storage.upload_url(
                attempt.object_key, body.size_bytes, body.checksum_sha256, seconds
            )
            return {
                "uploadUrl": url,
                "urlExpiresAt": iso(self.clock() + seconds * 1000),
                "headers": headers,
            }

    def complete(self, access: Access, job_id: str, token: str) -> dict:
        with self.sessions.begin() as session:
            job, attempt = self._leased(session, access, job_id, token, completed=True)
            if job.status == "READY":
                return {"status": "READY"}
            if not attempt.checksum:
                raise Problem(409, "UPLOAD_NOT_REGISTERED", "업로드 조건을 먼저 등록해야 합니다")
            self.storage.verify(attempt.object_key, attempt.size_bytes, attempt.checksum)
            # Recheck time after an external request before recording successful completion.
            if min(attempt.lease_until, job.deadline) <= self.clock():
                raise Problem(409, "LEASE_EXPIRED", "작업 lease가 만료되었습니다")
            job.status = "READY"
            job.object_expires_at = self.clock() + self.settings.clip_retention_seconds * 1000
            self._audit(session, access, "clip.complete", job.id)
        self._delete_receipt(access.agent.queue_url, attempt.receipt)
        return {"status": "READY"}

    def fail(self, access: Access, job_id: str, body: FailureBody) -> dict:
        with self.sessions.begin() as session:
            job, attempt = self._leased(session, access, job_id, body.lease_token)
            retryable = body.failure_code != "SOURCE_NOT_FOUND"
            self._retry(job, body.failure_code, retryable)
            status = job.status
        self._delete_receipt(access.agent.queue_url, attempt.receipt)
        self._try_dispatch(job_id)
        return {"status": status}

    def _delete_receipt(self, queue: str, receipt: str) -> None:
        try:
            self.queue.delete(queue, receipt)
        except Exception:
            emit_event("queue_ack_pending", level=logging.WARNING)

    def maintain(self) -> dict:
        expired_lease = exists(
            select(Attempt.id).where(
                Attempt.request_id == FileJob.id,
                Attempt.number == FileJob.attempt,
                Attempt.lease_until <= self.clock(),
            )
        )
        actionable = or_(
            and_(FileJob.status.in_(["QUEUED", "UPLOADING"]), FileJob.deadline <= self.clock()),
            and_(FileJob.status == "UPLOADING", expired_lease),
            and_(FileJob.status == "READY", FileJob.object_expires_at <= self.clock()),
            and_(FileJob.status == "QUEUED", FileJob.dispatched_version < FileJob.dispatch_version),
        )
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(FileJob.id).where(actionable).order_by(FileJob.deadline).limit(1000)
                )
            )
        for job_id in ids:
            with self.sessions.begin() as session:
                job = session.scalar(select(FileJob).where(FileJob.id == job_id).with_for_update())
                self._expire(session, job)
            self._try_dispatch(job_id)
        deleted = 0
        with self.sessions() as session:
            attempt_ids = list(
                session.scalars(
                    select(Attempt.id)
                    .join(FileJob, FileJob.id == Attempt.request_id)
                    .where(
                        Attempt.deleted_at.is_(None),
                        FileJob.deadline + self.settings.put_url_seconds * 1000 <= self.clock(),
                        or_(
                            Attempt.number < FileJob.attempt,
                            FileJob.status.in_(["FAILED", "EXPIRED"]),
                        ),
                    )
                    .order_by(Attempt.id)
                    .limit(1000)
                )
            )
        for attempt_id in attempt_ids:
            with self.sessions.begin() as session:
                attempt = session.get(Attempt, attempt_id)
                job = session.scalar(
                    select(FileJob).where(FileJob.id == attempt.request_id).with_for_update()
                )
                # Expired PUT URLs cannot recreate an already cleaned object.
                safe_at = job.deadline + self.settings.put_url_seconds * 1000
                keep = attempt.number == job.attempt and job.status in {"UPLOADING", "READY"}
                if not keep and safe_at <= self.clock():
                    self.storage.delete(attempt.object_key)
                    attempt.deleted_at = self.clock()
                    deleted += 1
        emit_event("maintenance_completed", jobsChecked=len(ids), objectsDeleted=deleted)
        return {"jobsChecked": len(ids), "objectsDeleted": deleted}
