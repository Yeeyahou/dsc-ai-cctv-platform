"""Disposable loopback fixture only. Never deploy this synthetic OAuth provider."""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import SecretStr

from dsc.backend.admin import Registration, register
from dsc.backend.api import create_app
from dsc.backend.config import Settings
from dsc.backend.db import Base, LiveSession, Principal, database
from dsc.backend.service import Platform
from dsc.shared.observability import contextual
from dsc.streaming.config import GatewaySettings
from dsc.streaming.main import serve


def asset():
    return {
        "assetId": "camera-1",
        "siteId": "site-1",
        "assetType": "camera",
        "displayName": "합성 검수 카메라",
        "aas": {
            "aasId": "urn:dsc:camera-1",
            "globalAssetId": "urn:dsc:asset:camera-1",
            "idShort": "camera_1",
        },
    }


root = Path("/tmp/live-rehearsal")
root.mkdir(exist_ok=True)
engine, sessions = database(f"sqlite:///{root}/backend.sqlite")
Base.metadata.create_all(engine)
port = int(os.environ.get("DSC_TEST_BROWSER_PORT", "18090"))
browser = f"http://127.0.0.1:{port}"
manifest = {
    "assets": [asset()],
    "principals": [
        {"principalId": "client:field", "scopes": ["dsc/ingest.write", "dsc/edge-server.work"]},
        {
            "principalId": "user:staff",
            "scopes": ["dsc/live.read"],
            "sites": ["site-1"],
            "video": True,
        },
    ],
    "edgeServers": [
        {
            "edgeServerId": "field",
            "principalId": "client:field",
            "siteId": "site-1",
            "assets": ["camera-1"],
            "role": "field",
            "queueUrl": "https://sqs.test/field",
        }
    ],
    "liveCameras": [
        {
            "assetId": "camera-1",
            "edgeServerId": "field",
            "path": "camera-1",
            "gatewayUrl": "https://synthetic.test",
        }
    ],
}
register(sessions, Registration.model_validate(manifest))
# The public backend contract requires HTTPS. The fixture rewrites only its test response URL.
clock_offset = 0


def clock():
    return int(time.time() * 1000) + clock_offset


settings = Settings(
    environment="test",
    live_enabled=True,
    allowed_origins=[browser],
    cursor_secret=SecretStr("test-only-cursor-secret-32-characters"),
)
platform = Platform(sessions, settings, None, None, clock)


def claims(token):
    if token not in {"staff", "field"}:
        from dsc.backend.errors import Problem

        raise Problem(401, "INVALID_TOKEN", "synthetic fixture token required")
    data = {
        "sub": token,
        "client_id": token,
        "scope": "dsc/live.read dsc/edge-server.work",
        "exp": int(time.time()) + 900,
    }
    if token == "staff":
        data["username"] = "staff"
    return data


app = create_app(platform, claims)
codes = {}


@app.middleware("http")
async def synthetic_endpoint(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith("/sessions") and response.status_code == 201:
        payload = b"".join([chunk async for chunk in response.body_iterator])
        value = json.loads(payload)
        value["whepUrl"] = browser + "/media/camera-1/whep"
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return JSONResponse(value, status_code=201, headers=headers)
    return response


@app.get("/oauth2/authorize")
def authorize(request: Request):
    params = request.query_params
    code = secrets.token_urlsafe(16)
    codes[code] = params["code_challenge"]
    return RedirectResponse(browser + "/?" + urlencode({"code": code, "state": params["state"]}))


@app.post("/oauth2/token")
async def token(request: Request):
    from urllib.parse import parse_qs

    fields = parse_qs((await request.body()).decode())
    if fields.get("grant_type") == ["client_credentials"]:
        if (
            request.headers.get("authorization")
            != "Basic " + base64.b64encode(b"field:synthetic-only").decode()
        ):
            return JSONResponse({}, status_code=401)
        who = "field"
    else:
        verifier = fields.get("code_verifier", [""])[0]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        if codes.pop(fields.get("code", [""])[0], None) != challenge:
            return JSONResponse({}, status_code=401)
        who = "staff"
    return {"access_token": who, "token_type": "Bearer", "expires_in": 900}


@app.post("/test/revoke")
def revoke():
    with sessions.begin() as session:
        session.get(Principal, "user:staff").active = False
    return {}


@app.post("/test/expire")
def expire():
    with sessions.begin() as session:
        for row in session.query(LiveSession):
            row.expires_at = clock() - 1
    return {}


@app.post("/test/reset")
def reset():
    with sessions.begin() as session:
        session.get(Principal, "user:staff").active = True
    return {}


server = uvicorn.Server(
    uvicorn.Config(app, host="0.0.0.0", port=18080, access_log=False, log_level="warning")
)
thread = threading.Thread(target=contextual(server.run), daemon=True)
thread.start()
secret = root / "secret"
secret.write_text("synthetic-only")
secret.chmod(0o600)
try:
    serve(
        GatewaySettings(
            environment="test",
            api_url="http://127.0.0.1:18080",
            token_url="http://127.0.0.1:18080/oauth2/token",
            client_id="field",
            secret_file=secret,
            oauth_origin="http://127.0.0.1:18080",
            staff_client_id="staff",
            browser_origin=browser,
            allowed_gateway_origins=[browser],
            state_dir=root / "gateway",
            mediamtx_config=Path("/fixture/mediamtx.json"),
            poll_seconds=1,
        )
    )
finally:
    server.should_exit = True
    thread.join(timeout=5)
