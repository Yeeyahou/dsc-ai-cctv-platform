import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from dsc.backend.admin import Registration, register
from dsc.backend.db import EdgeServer, LiveCamera, LiveSession, Principal
from tests.conftest import headers


@pytest.fixture
def live_system(system):
    system.platform.settings.live_enabled = True
    with system.sessions.begin() as session:
        session.get(EdgeServer, "edge-server-1").role = "field"
        for identity in ["user:staff", "user:partial"]:
            row = session.get(Principal, identity)
            row.scopes = [*row.scopes, "dsc/live.read"]
    register(
        system.sessions,
        Registration.model_validate(
            {
                "liveCameras": [
                    {
                        "assetId": "camera-1",
                        "edgeServerId": "edge-server-1",
                        "path": "camera-1",
                        "gatewayUrl": "https://live.test",
                    }
                ]
            }
        ),
    )

    # The injected verifier has the same boundary as a verified Cognito claim set.
    def claims(token):
        scopes = "dsc/live.read dsc/edge-server.work dsc/aas.read dsc/timeseries.read"
        result = {
            "sub": token,
            "client_id": token,
            "scope": scopes,
            "exp": system.clock() // 1000 + 120,
        }
        if token in {"staff", "partial"}:
            result["username"] = token
        return result

    from fastapi.testclient import TestClient

    from dsc.backend.api import create_app

    with TestClient(create_app(system.platform, claims), raise_server_exceptions=False) as client:
        system.live_client = client
        yield system


def start(system, token="staff"):
    response = system.live_client.post(
        "/live/cameras/camera-1/sessions", json={}, headers=headers(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorize(system, value, token="edge", path="camera-1"):
    return system.live_client.post(
        "/edge-server/live/authorize",
        headers=headers(token),
        json={"token": value["sessionToken"], "path": path},
    )


def test_live_camera_permissions_and_capability_binding(live_system):
    s = live_system
    assert s.live_client.get("/live/cameras").status_code == 401
    assert s.live_client.get("/live/cameras", headers=headers("partner")).status_code == 403
    assert (
        s.live_client.get("/live/cameras", headers=headers("staff")).json()["items"][0]["assetId"]
        == "camera-1"
    )
    value = start(s)
    assert value["whepUrl"] == "https://live.test/media/camera-1/whep"
    assert authorize(s, value).status_code == 200
    assert authorize(s, value, token="edge-other").status_code == 404
    assert authorize(s, value, path="other").status_code == 404
    with s.sessions() as session:
        row = session.get(LiveSession, value["sessionId"])
        assert row.token_hash == hashlib.sha256(value["sessionToken"].encode()).hexdigest()
        assert row.expires_at == s.clock() + 120000
        assert value["sessionToken"] not in str(row.__dict__)
    bad = {**value, "sessionToken": value["sessionToken"][:-1] + "!"}
    assert authorize(s, bad).status_code == 404


@pytest.mark.parametrize(
    "mutation",
    [
        "expiry",
        "principal",
        "video",
        "scope",
        "asset",
        "source",
        "camera",
        "assignment",
        "disabled",
    ],
)
def test_live_existing_connections_lose_authority(live_system, mutation):
    s = live_system
    value = start(s)
    with s.sessions.begin() as session:
        person = session.get(Principal, "user:staff")
        if mutation == "expiry":
            s.clock.advance(121)
        elif mutation == "principal":
            person.active = False
        elif mutation == "video":
            person.video = False
        elif mutation == "scope":
            person.scopes = []
        elif mutation == "asset":
            person.sites, person.assets = [], []
        elif mutation == "source":
            session.get(Principal, "client:edge").active = False
        elif mutation == "camera":
            session.get(LiveCamera, "camera-1").active = False
        elif mutation == "assignment":
            session.get(EdgeServer, "edge-server-1").assets = []
        elif mutation == "disabled":
            s.platform.settings.live_enabled = False
    response = s.live_client.post(
        "/edge-server/live/check",
        headers=headers("edge"),
        json={"sessionIds": [value["sessionId"]]},
    )
    assert response.status_code != 200 or response.json()["activeSessionIds"] == []
    assert authorize(s, value).status_code != 200


def test_stop_is_owned_idempotent_and_durable(live_system):
    s = live_system
    value = start(s)
    path = f"/live/sessions/{value['sessionId']}/stop"
    assert s.live_client.post(path, json={}, headers=headers("partial")).status_code == 404
    for _ in range(2):
        assert s.live_client.post(path, json={}, headers=headers()).status_code == 200
    assert authorize(s, value).status_code == 404
    assert (
        s.live_client.post(f"/live/sessions/{uuid4()}/stop", json={}, headers=headers()).status_code
        == 404
    )


def test_camera_registration_change_revokes_active_sessions(live_system):
    s = live_system
    value = start(s)
    register(
        s.sessions,
        Registration.model_validate(
            {
                "liveCameras": [
                    {
                        "assetId": "camera-1",
                        "edgeServerId": "edge-server-1",
                        "path": "camera-new",
                        "gatewayUrl": "https://live.test",
                    }
                ]
            }
        ),
    )
    assert authorize(s, value).status_code == 404
    with pytest.raises((ValueError, ValidationError)):
        register(
            s.sessions,
            Registration.model_validate(
                {
                    "liveCameras": [
                        {
                            "assetId": "other-camera",
                            "edgeServerId": "edge-server-1",
                            "path": "other",
                            "gatewayUrl": "https://live.test",
                        }
                    ]
                }
            ),
        )
    with s.sessions() as session:
        assert list(session.scalars(select(LiveCamera.asset_id))) == ["camera-1"]


def test_scope_does_not_grant_other_site_or_video_access(live_system):
    s = live_system
    with s.sessions.begin() as session:
        staff = session.get(Principal, "user:staff")
        staff.sites = ["other-site"]
    assert s.live_client.get("/live/cameras", headers=headers()).json() == {"items": []}
    assert (
        s.live_client.post(
            "/live/cameras/camera-1/sessions", json={}, headers=headers()
        ).status_code
        == 404
    )


def test_live_disabled_by_default(system):
    assert system.client.get("/live/cameras", headers=headers()).status_code == 503
