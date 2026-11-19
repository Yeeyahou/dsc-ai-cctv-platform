"""MediaMTX authentication callback and active connection revocation."""

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from dsc.backend.observability import LoggedRoute, RequestLogging
from dsc.edge.client import EdgeClient
from dsc.shared.observability import emit_event, log_methods
from dsc.streaming.config import GatewaySettings


class MediaAuthorization(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    protocol: str
    id: UUID
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    token: SecretStr = Field(max_length=128)
    query: str = ""


@log_methods(level=logging.DEBUG)
class Gateway:
    def __init__(self, settings: GatewaySettings, backend=None, media=None):
        self.settings = settings
        self.backend = backend or EdgeClient(settings, scopes="dsc/edge-server.work")
        self.media = media or httpx.Client(
            base_url="http://127.0.0.1:9997",
            timeout=3,
            follow_redirects=False,
            trust_env=False,
        )
        settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.state_dir / "peers.sqlite"
        self.lock = threading.Lock()
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS peers "
                "(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, path TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def authorize(self, body: MediaAuthorization) -> None:
        if (
            body.action != "read"
            or body.protocol != "webrtc"
            or body.query
            or not re.fullmatch(r"[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}", body.token.get_secret_value())
        ):
            raise HTTPException(403, "시청 권한이 없습니다")
        try:
            value = self.backend.post(
                "/edge-server/live/authorize",
                {
                    "token": body.token.get_secret_value(),
                    "path": body.path,
                },
            )
            session_id = str(UUID(value["sessionId"]))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(403, "시청 권한을 확인할 수 없습니다") from exc
        with self.lock, self._db() as conn:
            prior = conn.execute(
                "SELECT session_id, path FROM peers WHERE id = ?", (str(body.id),)
            ).fetchone()
            if prior is not None:
                if prior != (session_id, body.path):
                    raise HTTPException(403, "다른 시청 세션입니다")
                return
            if conn.execute("SELECT count(*) FROM peers").fetchone()[0] >= self.settings.max_peers:
                raise HTTPException(429, "동시 시청 한도를 초과했습니다")
            conn.execute(
                "INSERT INTO peers VALUES (?, ?, ?)", (str(body.id), session_id, body.path)
            )

    def reconcile(self) -> None:
        with self.lock:
            self._reconcile()

    def _reconcile(self) -> None:
        response = self.media.get("/v3/webrtc/sessions/list", params={"itemsPerPage": 1000})
        response.raise_for_status()
        peers = response.json()["items"]
        # A missing page must never leave an established connection unchecked.
        if response.json().get("pageCount", 1) > 1 or len(peers) > 1000:
            raise RuntimeError("unexpected media session count")
        with self._db() as conn:
            bindings = {row[0]: row[1:] for row in conn.execute("SELECT * FROM peers")}
            identities = list({binding[0] for binding in bindings.values()})
            try:
                active = (
                    set(
                        self.backend.post(
                            "/edge-server/live/check",
                            {
                                "sessionIds": identities,
                            },
                        )["activeSessionIds"]
                    )
                    if identities
                    else set()
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                active = set()
                emit_event("live_authorization_unavailable", level=logging.WARNING)
            present = set()
            for peer in peers:
                identity = str(UUID(peer["id"]))
                present.add(identity)
                binding = bindings.get(identity)
                if (
                    binding
                    and binding[0] in active
                    and (
                        not peer.get("peerConnectionEstablished") or binding[1] == peer.get("path")
                    )
                ):
                    continue
                if not binding and not peer.get("peerConnectionEstablished"):
                    continue
                kicked = self.media.post(f"/v3/webrtc/sessions/kick/{identity}")
                if kicked.status_code != 404:
                    kicked.raise_for_status()
                emit_event("live_peer_disconnected", state="REVOKED", mediaPeerId=identity)
            for identity in bindings.keys() - present:
                conn.execute("DELETE FROM peers WHERE id = ?", (identity,))

    def close(self) -> None:
        self.media.close()
        self.backend.close()


def create_app(gateway: Gateway) -> FastAPI:
    from dsc.backend.api import BodyLimit

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.router.route_class = LoggedRoute
    app.add_middleware(BodyLimit)
    app.add_middleware(RequestLogging, inherit_trace=False)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "인증 요청 형식이 올바르지 않습니다"}, status_code=422)

    @app.post("/internal/authorize")
    def authorize(body: MediaAuthorization, request: Request):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403)
        gateway.authorize(body)
        return {}

    @app.get("/config.json")
    def config():
        settings = gateway.settings
        return {
            "apiUrl": settings.api_url.rstrip("/"),
            "oauthOrigin": settings.oauth_origin,
            "clientId": settings.staff_client_id,
            "redirectUri": settings.browser_origin.rstrip("/") + "/",
            "gatewayOrigins": settings.allowed_gateway_origins,
        }

    @app.get("/{asset}")
    @app.get("/")
    def ui(asset: str = "index.html"):
        if asset not in {"index.html", "app.js", "style.css"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).parent / "web" / asset)

    return app
