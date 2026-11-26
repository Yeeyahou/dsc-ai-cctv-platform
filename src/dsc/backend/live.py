"""Live viewing permissions and durable, camera-bound capabilities."""

import hashlib
import hmac
import secrets
from uuid import UUID, uuid4

from pydantic import Field, SecretStr
from sqlalchemy import select

from dsc.backend.auth import Access
from dsc.backend.db import Asset, Audit, EdgeServer, LiveCamera, LiveSession, Principal
from dsc.backend.errors import Problem, missing
from dsc.shared.contracts import Contract, iso
from dsc.shared.observability import log_methods


class LiveAuthorization(Contract):
    token: SecretStr = Field(min_length=64, max_length=128)
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class LiveCheck(Contract):
    session_ids: list[UUID] = Field(max_length=64)


@log_methods()
class Live:
    def __init__(self, platform):
        self.platform = platform
        self.sessions, self.clock = platform.sessions, platform.clock

    def _enabled(self) -> None:
        if not self.platform.settings.live_enabled:
            raise Problem(503, "LIVE_DISABLED", "실시간 시청이 활성화되지 않았습니다")

    def _viewer(self, session, principal_id: str, camera: LiveCamera) -> bool:
        principal = session.get(Principal, principal_id)
        asset = session.get(Asset, camera.asset_id)
        edge = session.get(EdgeServer, camera.edge_server_id)
        source = session.get(Principal, edge.principal_id) if edge else None
        return bool(
            camera.active
            and principal
            and principal.active
            and principal.video
            and "dsc/live.read" in principal.scopes
            and asset
            and (asset.site_id in principal.sites or asset.id in principal.assets)
            and edge
            and edge.role == "field"
            and edge.site_id == asset.site_id
            and asset.id in edge.assets
            and source
            and source.active
            and "dsc/edge-server.work" in source.scopes
        )

    def cameras(self, actor: Access) -> dict:
        self._enabled()
        actor.require("dsc/live.read")
        with self.sessions() as session:
            return {
                "items": [
                    {
                        "assetId": camera.asset_id,
                        "siteId": session.get(Asset, camera.asset_id).site_id,
                        "displayName": session.get(Asset, camera.asset_id).display_name,
                    }
                    for camera in session.scalars(select(LiveCamera).order_by(LiveCamera.asset_id))
                    if self._viewer(session, actor.principal.id, camera)
                ]
            }

    def create(self, actor: Access, asset_id: str) -> dict:
        self._enabled()
        actor.require("dsc/live.read")
        now = self.clock()
        with self.sessions.begin() as session:
            camera = session.get(LiveCamera, asset_id)
            if camera is None or not self._viewer(session, actor.principal.id, camera):
                raise missing()
            identity, secret = str(uuid4()), secrets.token_urlsafe(32)
            token = f"{identity}.{secret}"
            until = min(
                now + self.platform.settings.live_session_seconds * 1000,
                actor.expires_at if actor.expires_at is not None else now + 900000,
            )
            if until <= now:
                raise Problem(401, "INVALID_TOKEN", "로그인이 만료되었습니다")
            session.add(
                LiveSession(
                    id=identity,
                    asset_id=asset_id,
                    principal_id=actor.principal.id,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    created_at=now,
                    expires_at=until,
                )
            )
            session.execute(
                Audit.__table__.insert()
                .inline()
                .values(
                    principal_id=actor.principal.id,
                    action="live.start",
                    object_id=identity,
                    at=now,
                )
            )
            return {
                "sessionId": identity,
                "assetId": asset_id,
                "sessionToken": token,
                "expiresAt": iso(until),
                "whepUrl": f"{camera.gateway_url}/media/{camera.path}/whep",
            }

    def stop(self, actor: Access, session_id: str) -> dict:
        actor.require("dsc/live.read")
        with self.sessions.begin() as session:
            row = session.get(LiveSession, session_id)
            if row is None or row.principal_id != actor.principal.id:
                raise missing()
            if row.revoked_at is None:
                row.revoked_at = self.clock()
                session.execute(
                    Audit.__table__.insert()
                    .inline()
                    .values(
                        principal_id=actor.principal.id,
                        action="live.stop",
                        object_id=session_id,
                        at=self.clock(),
                    )
                )
            return {"sessionId": session_id, "status": "STOPPED"}

    def _valid(self, session, actor: Access, row: LiveSession | None) -> bool:
        camera = session.get(LiveCamera, row.asset_id) if row else None
        return bool(
            row
            and row.revoked_at is None
            and row.expires_at > self.clock()
            and camera
            and actor.edge_server
            and camera.edge_server_id == actor.edge_server.id
            and self._viewer(session, row.principal_id, camera)
        )

    def authorize(self, actor: Access, body: LiveAuthorization) -> dict:
        self._enabled()
        actor.require("dsc/edge-server.work")
        token = body.token.get_secret_value()
        try:
            identity = str(UUID(token.split(".")[0]))
        except ValueError as exc:
            raise missing() from exc
        with self.sessions() as session:
            row = session.get(LiveSession, identity)
            if not self._valid(session, actor, row):
                raise missing()
            camera = session.get(LiveCamera, row.asset_id)
            if camera.path != body.path or not hmac.compare_digest(
                row.token_hash, hashlib.sha256(token.encode()).hexdigest()
            ):
                raise missing()
            return {"sessionId": row.id, "expiresAt": iso(row.expires_at)}

    def check(self, actor: Access, body: LiveCheck) -> dict:
        self._enabled()
        actor.require("dsc/edge-server.work")
        with self.sessions() as session:
            return {
                "activeSessionIds": [
                    str(identity)
                    for identity in body.session_ids
                    if self._valid(session, actor, session.get(LiveSession, str(identity)))
                ]
            }
