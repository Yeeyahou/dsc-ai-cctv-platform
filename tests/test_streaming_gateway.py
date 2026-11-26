import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dsc.streaming.config import GatewaySettings
from dsc.streaming.gateway import Gateway, MediaAuthorization, create_app
from dsc.streaming.install import Installation, media_config


class Backend:
    def __init__(self):
        self.session_id = str(uuid4())
        self.active, self.fail = True, False

    def post(self, path, body):
        if self.fail:
            raise httpx.ConnectError("private source URL must not appear in logs")
        if path.endswith("authorize"):
            return {"sessionId": self.session_id}
        return {"activeSessionIds": [self.session_id] if self.active else []}

    def close(self):
        pass


@pytest.fixture
def gateway(tmp_path):
    backend = Backend()
    state = {"peers": [], "kicked": []}

    def media(request):
        if request.url.path.endswith("list"):
            return httpx.Response(200, json={"items": state["peers"], "pageCount": 1})
        state["kicked"].append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200)

    settings = GatewaySettings(
        api_url="https://api.test",
        token_url="https://auth.test/oauth2/token",
        client_id="field",
        secret_file=tmp_path / "secret",
        oauth_origin="https://auth.test",
        staff_client_id="staff",
        browser_origin="https://live.test",
        allowed_gateway_origins=["https://live.test"],
        state_dir=tmp_path / "state",
    )
    service = Gateway(
        settings,
        backend,
        httpx.Client(base_url="http://127.0.0.1:9997", transport=httpx.MockTransport(media)),
    )
    yield service, backend, state
    service.close()


def body(identity=None):
    return {
        "action": "read",
        "protocol": "webrtc",
        "id": identity or str(uuid4()),
        "path": "camera-1",
        "token": str(uuid4()) + "." + "a" * 43,
    }


def test_callback_and_config_do_not_expose_secrets(gateway, operation_logs):
    service, backend, state = gateway
    value = body()
    with TestClient(create_app(service)) as client:
        assert client.post("/internal/authorize", json=value).status_code == 200
        assert client.get("/").status_code == 200
        config = client.get("/config.json").json()
        assert "secret" not in json.dumps(config)
        assert client.get("/not-a-file").status_code == 404
    assert value["token"] not in json.dumps(operation_logs)
    assert "peers.sqlite" not in json.dumps(operation_logs)


@pytest.mark.parametrize(
    "key,value",
    [("action", "publish"), ("protocol", "rtsp"), ("query", "token=private"), ("token", "short")],
)
def test_other_protocols_and_url_tokens_are_rejected(gateway, key, value):
    service, _, _ = gateway
    data = body()
    data[key] = value
    with pytest.raises(HTTPException):
        service.authorize(MediaAuthorization.model_validate(data))


@pytest.mark.parametrize("reason", ["revoked", "outage", "unknown", "wrong-path"])
def test_established_peers_are_kicked_and_state_survives_restart(gateway, reason):
    service, backend, state = gateway
    identity = str(uuid4())
    service.authorize(MediaAuthorization.model_validate(body(identity)))
    state["peers"] = [{"id": identity, "path": "camera-1", "peerConnectionEstablished": True}]
    service.reconcile()
    assert state["kicked"] == []
    # Reopen the persistent registry to represent a gateway process restart.
    restored = Gateway(service.settings, backend, service.media)
    if reason == "revoked":
        backend.active = False
    elif reason == "outage":
        backend.fail = True
    elif reason == "unknown":
        state["peers"][0]["id"] = str(uuid4())
    elif reason == "wrong-path":
        state["peers"][0]["path"] = "other"
    restored.reconcile()
    assert state["kicked"] == [state["peers"][0]["id"]]


def test_peer_limit_and_rebinding(gateway):
    service, backend, _ = gateway
    service.settings.max_peers = 1
    data = MediaAuthorization.model_validate(body())
    service.authorize(data)
    service.authorize(data)
    with pytest.raises(HTTPException):
        service.authorize(MediaAuthorization.model_validate(body()))
    backend.session_id = str(uuid4())
    with pytest.raises(HTTPException):
        service.authorize(data)


def test_media_config_keeps_credentials_local_and_disables_other_protocols(tmp_path):
    secret = tmp_path / "camera.json"
    secret.write_text('{"username":"read","password":"special@pass:/"}')
    secret.chmod(0o600)
    install = Installation(
        vpn_address="100.64.0.10",
        browser_origin="https://live.test",
        cameras=[
            {
                "path": "camera-1",
                "rtsp_url": "rtsp://192.168.1.64/Streaming/Channels/102",
                "credentials_file": secret,
            }
        ],
    )
    config = media_config(install)
    assert "special%40pass%3A%2F@" in config["paths"]["camera-1"]["source"]
    assert config["paths"]["camera-1"]["sourceOnDemand"]
    assert config["authHTTPExclude"] == [{"action": "api"}]
    assert all(config[key] is False for key in ["rtsp", "hls", "rtmp", "srt", "playback"])
    secret.chmod(0o644)
    with pytest.raises(PermissionError):
        media_config(install)
