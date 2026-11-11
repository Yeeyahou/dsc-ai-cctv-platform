"""Verified clip copies and bounded capabilities for field-to-office transfers."""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from dsc.backend.auth import Access
from dsc.backend.db import (
    ArchiveTransfer,
    ClipReplica,
    EdgeServer,
    Event,
    EventAsset,
    EventMedia,
    FileJob,
    Principal,
)
from dsc.backend.errors import Problem, missing
from dsc.shared.contracts import ReplicaBody, TransferReceipt, digest, iso, milliseconds
from dsc.shared.observability import log_methods

if TYPE_CHECKING:
    from dsc.backend.service import Platform


def uploader_allowed(session: Session, server: EdgeServer, event: Event) -> bool:
    principal = session.get(Principal, server.principal_id)
    linked = set(
        session.scalars(select(EventAsset.asset_id).where(EventAsset.event_id == event.id))
    )
    return bool(
        principal
        and principal.active
        and "dsc/edge-server.work" in principal.scopes
        and server.site_id == event.site_id
        and linked
        and linked.issubset(server.assets)
    )


@log_methods(level=logging.DEBUG)
class Replicas:
    def __init__(self, platform: Platform):
        self.platform = platform
        self.sessions, self.clock = platform.sessions, platform.clock

    def _server(self, session: Session, access: Access) -> EdgeServer:
        access.require("dsc/edge-server.work")
        row = session.get(EdgeServer, access.edge_server.id) if access.edge_server else None
        principal = session.get(Principal, row.principal_id) if row else None
        if (
            not row
            or not principal
            or not principal.active
            or "dsc/edge-server.work" not in principal.scopes
        ):
            raise missing()
        return row

    def _content(
        self, session: Session, access: Access, body: ReplicaBody
    ) -> tuple[EdgeServer, Event, ClipReplica]:
        server = self._server(session, access)
        event = session.get(Event, str(body.event_id))
        if (
            event is None
            or event.edge_server_id != server.id
            or not uploader_allowed(session, server, event)
        ):
            raise missing()
        self.platform._lock_event_media(session, event.id)
        media = session.get(EventMedia, event.id)
        key = (event.id, body.revision, server.id)
        row = session.get(ClipReplica, key)
        if (
            media is None
            or media.clip_id != str(body.clip_id)
            or (
                row is None
                and (
                    media.revision != body.revision
                    or media.payload["status"] != "AVAILABLE"
                    or media.payload["sizeBytes"] != body.size_bytes
                )
            )
        ):
            raise Problem(
                409, "REPLICA_METADATA_CONFLICT", "등록된 클립 revision과 크기가 다릅니다"
            )
        copies = list(session.scalars(select(ClipReplica).where(ClipReplica.event_id == event.id)))
        if any(
            copy.checksum != body.checksum_sha256 or copy.size_bytes != body.size_bytes
            for copy in copies
        ):
            raise Problem(409, "REPLICA_CONTENT_CONFLICT", "동일 clipId의 클립 내용은 불변입니다")
        if row is None:
            row = ClipReplica(
                event_id=event.id,
                revision=body.revision,
                edge_server_id=server.id,
                clip_id=str(body.clip_id),
                size_bytes=body.size_bytes,
                checksum=body.checksum_sha256,
                present=True,
                verified_at=self.clock(),
                created_at=milliseconds(
                    datetime.fromisoformat(media.payload["endedAt"].replace("Z", "+00:00"))
                ),
            )
            session.add(row)
        elif not row.present:
            raise Problem(409, "REPLICA_RETIRED", "정리된 사본은 다시 등록할 수 없습니다")
        else:
            row.verified_at = self.clock()
        return server, event, row

    def register(self, access: Access, body: ReplicaBody) -> dict:
        with self.sessions.begin() as session:
            server, _, row = self._content(session, access, body)
            return {
                "eventId": row.event_id,
                "clipId": row.clip_id,
                "revision": row.revision,
                "edgeServerId": server.id,
                "sizeBytes": row.size_bytes,
                "checksumSha256": row.checksum,
                "status": "PRESENT",
            }

    def prepare(self, access: Access, body: ReplicaBody) -> dict:
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            source, event, replica = self._content(session, access, body)
            target = (
                session.get(EdgeServer, source.archive_server_id)
                if source.archive_server_id
                else None
            )
            if (
                not target
                or target.role != "archive"
                or not target.archive_url
                or not uploader_allowed(session, target, event)
            ):
                raise Problem(
                    409, "ARCHIVE_NOT_CONFIGURED", "허용된 사무실 수신 워커 등록이 필요합니다", True
                )
            row = ArchiveTransfer(
                id=str(uuid4()),
                event_id=event.id,
                clip_id=replica.clip_id,
                revision=replica.revision,
                source_id=source.id,
                receiver_id=target.id,
                size_bytes=replica.size_bytes,
                checksum=replica.checksum,
                token_hash=digest(token),
                expires_at=self.clock() + self.platform.settings.archive_transfer_seconds * 1000,
                created_at=replica.created_at,
            )
            session.add(row)
            return {
                "createdAt": iso(row.created_at),
                "transferId": row.id,
                "transferToken": token,
                "receiverId": target.id,
                "uploadUrl": f"{target.archive_url}/archive/transfers/{row.id}",
                "expiresAt": iso(row.expires_at),
            }

    def _transfer(self, session, access: Access, transfer_id: str, token: str, completed=False):
        receiver = self._server(session, access)
        row = session.scalar(
            select(ArchiveTransfer).where(ArchiveTransfer.id == transfer_id).with_for_update()
        )
        if (
            row is None
            or row.receiver_id != receiver.id
            or not hmac.compare_digest(row.token_hash, digest(token))
        ):
            raise missing()
        event = session.get(Event, row.event_id)
        source = session.get(EdgeServer, row.source_id)
        if (
            receiver.role != "archive"
            or source.archive_server_id != receiver.id
            or not uploader_allowed(session, source, event)
            or not uploader_allowed(session, receiver, event)
        ):
            raise missing()
        if row.expires_at <= self.clock() and not (completed and row.completed_at is not None):
            raise Problem(409, "TRANSFER_EXPIRED", "클립 전송 권한이 만료되었습니다", True)
        return row

    @staticmethod
    def _receipt(row: ArchiveTransfer) -> dict:
        return {
            "createdAt": iso(row.created_at),
            "transferId": row.id,
            "eventId": row.event_id,
            "clipId": row.clip_id,
            "revision": row.revision,
            "sourceId": row.source_id,
            "receiverId": row.receiver_id,
            "sizeBytes": row.size_bytes,
            "checksumSha256": row.checksum,
            "status": "STORED" if row.completed_at is not None else "PENDING",
        }

    def authorize(self, access: Access, transfer_id: str, token: str) -> dict:
        with self.sessions.begin() as session:
            return self._receipt(self._transfer(session, access, transfer_id, token))

    def complete(self, access: Access, transfer_id: str, body: TransferReceipt) -> dict:
        with self.sessions.begin() as session:
            row = self._transfer(session, access, transfer_id, body.transfer_token, completed=True)
            if row.size_bytes != body.size_bytes or row.checksum != body.checksum_sha256:
                raise Problem(
                    409, "ARCHIVE_VERIFICATION_FAILED", "NAS 사본의 크기 또는 해시가 다릅니다"
                )
            self.platform._lock_event_media(session, row.event_id)
            source = session.get(ClipReplica, (row.event_id, row.revision, row.source_id))
            if source is None or source.checksum != row.checksum:
                raise Problem(409, "REPLICA_CONTENT_CONFLICT", "검증된 현장 사본이 필요합니다")
            replica = session.get(ClipReplica, (row.event_id, row.revision, row.receiver_id))
            if replica is None:
                session.add(
                    ClipReplica(
                        event_id=row.event_id,
                        clip_id=row.clip_id,
                        revision=row.revision,
                        edge_server_id=row.receiver_id,
                        size_bytes=row.size_bytes,
                        checksum=row.checksum,
                        present=True,
                        verified_at=self.clock(),
                        created_at=source.created_at,
                    )
                )
            elif replica.checksum != row.checksum or replica.size_bytes != row.size_bytes:
                raise Problem(409, "REPLICA_CONTENT_CONFLICT", "사무실 사본의 내용이 다릅니다")
            else:
                replica.present, replica.verified_at = True, self.clock()
            row.completed_at = row.completed_at or self.clock()
            return self._receipt(row)

    def retire(self, access: Access, body: ReplicaBody) -> dict:
        with self.sessions.begin() as session:
            source = self._server(session, access)
            event = session.get(Event, str(body.event_id))
            if (
                not event
                or event.edge_server_id != source.id
                or not uploader_allowed(session, source, event)
            ):
                raise missing()
            self.platform._lock_event_media(session, event.id)
            replica = session.get(ClipReplica, (event.id, body.revision, source.id))
            if (
                replica is None
                or replica.clip_id != str(body.clip_id)
                or replica.size_bytes != body.size_bytes
                or replica.checksum != body.checksum_sha256
            ):
                raise missing()
            if replica.present:
                target = (
                    session.get(EdgeServer, source.archive_server_id)
                    if source.archive_server_id
                    else None
                )
                copy = (
                    session.get(ClipReplica, (event.id, body.revision, target.id))
                    if target
                    else None
                )
                jobs = session.scalar(
                    select(FileJob.id)
                    .where(
                        FileJob.event_id == event.id, FileJob.status.in_(["QUEUED", "UPLOADING"])
                    )
                    .limit(1)
                )
                if (
                    replica.created_at + self.platform.settings.local_retention_days * 86400000
                    > self.clock()
                    or jobs is not None
                    or not copy
                    or not copy.present
                    or copy.checksum != replica.checksum
                    or copy.size_bytes != replica.size_bytes
                    or not self.platform.worker_healthy(target)
                    or not uploader_allowed(session, target, event)
                ):
                    raise Problem(
                        409,
                        "REPLICA_IN_USE",
                        "보관 기간, NAS 확인 또는 작업 보호 조건을 충족하지 않습니다",
                        True,
                    )
                replica.present = False
            return {
                "eventId": replica.event_id,
                "clipId": replica.clip_id,
                "revision": replica.revision,
                "sizeBytes": replica.size_bytes,
                "checksumSha256": replica.checksum,
                "status": "RETIRED",
            }
