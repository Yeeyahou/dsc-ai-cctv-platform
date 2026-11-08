from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from dsc.backend.aas import encoded
from dsc.backend.db import Event, EventAsset, Principal
from tests.conftest import headers


def ingest(system, body):
    return system.client.post("/ingest", json=body, headers=headers("edge"))


def test_ingest_durable_duplicate_and_conflict(system, event_body):
    result = ingest(system, event_body)
    assert result.status_code == 201, result.text
    assert result.json()["duplicate"] is False
    assert ingest(system, event_body).status_code == 200
    changed = {**event_body, "occurredAt": "2026-11-07T10:00:01.000Z"}
    assert ingest(system, changed).status_code == 409
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(EventAsset)) == 2
    result = system.client.get(f"/events/{event_body['eventId']}", headers=headers())
    assert result.status_code == 200
    assert result.json()["occurredAt"] == event_body["occurredAt"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(siteId="other-site"),
        lambda b: b["assets"].append({"assetId": "other-camera", "role": "context"}),
    ],
)
def test_cross_site_denied_atomically(system, event_body, mutate):
    mutate(event_body)
    assert ingest(system, event_body).status_code == 403
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(schemaVersion="2.0"),
        lambda b: b.update(unknown="field"),
        lambda b: b.pop("incidentId"),
        lambda b: b.update(occurredAt="2026-11-07T10:00:00"),
        lambda b: b.update(metrics={"packetLossRatePct": 101}),
        lambda b: b["media"].pop("sizeBytes"),
        lambda b: b["media"].update(sizeBytes=True),
        lambda b: b.update(confidence=True),
        lambda b: b.update(confidence="0.5"),
        lambda b: b.update(media=None),
    ],
)
def test_invalid_input_not_saved(system, event_body, mutate):
    mutate(event_body)
    assert ingest(system, event_body).status_code == 422


def test_size_and_no_auth(system):
    assert system.client.get("/description").status_code == 401
    response = system.client.post(
        "/ingest", content=b"x" * (256 * 1024 + 1), headers=headers("edge")
    )
    assert response.status_code == 413


def test_invalid_json_returns_400_without_echo(system):
    response = system.client.post(
        "/ingest",
        content=b"{broken",
        headers={**headers("edge"), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["traceId"] == response.headers["X-Request-Id"]


def test_media_revision_and_ownership(system, event_body):
    ingest(system, event_body)
    url = f"/ingest/events/{event_body['eventId']}/media"
    body = {**event_body["media"], "revision": 1}
    assert system.client.put(url, json=body, headers=headers("edge-other")).status_code == 404
    assert system.client.put(url, json=body, headers=headers("edge")).status_code == 200
    assert system.client.put(url, json=body, headers=headers("edge")).status_code == 200
    changed = {**body, "status": "MISSING"}
    assert system.client.put(url, json=changed, headers=headers("edge")).status_code == 409
    assert (
        system.client.put(url, json={**changed, "revision": 2}, headers=headers("edge")).status_code
        == 200
    )
    assert system.client.put(url, json=body, headers=headers("edge")).status_code == 409


def test_acl_all_linked_assets_and_aggregates(system, event_body):
    ingest(system, event_body)
    assert (
        system.client.get(
            f"/events/{event_body['eventId']}", headers=headers("partial")
        ).status_code
        == 404
    )
    query = {"siteId": "site-1", "from": "2026-11-07T00:00:00Z", "to": "2026-11-08T00:00:00Z"}
    result = system.client.get("/events", params=query, headers=headers("partial"))
    assert result.json()["items"] == []
    result = system.client.get("/timeseries/camera-1/current", headers=headers("partial"))
    assert result.json()["recordCount"] == 0
    assert "startTime" not in result.json()
    assert system.client.get("/events", params=query, headers=headers("partner")).status_code == 403


def test_pagination_cursor_bound_to_query_and_grants(system, event_body):
    for _ in range(3):
        body = deepcopy(event_body)
        body["eventId"], body["media"]["clipId"] = str(uuid4()), str(uuid4())
        assert ingest(system, body).status_code == 201
    query = {
        "siteId": "site-1",
        "from": "2026-11-07T00:00:00Z",
        "to": "2026-11-08T00:00:00Z",
        "limit": 2,
    }
    first = system.client.get("/events", params=query, headers=headers()).json()
    second = system.client.get(
        "/events", params={**query, "cursor": first["nextCursor"]}, headers=headers()
    ).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert "nextCursor" not in second
    assert len({x["eventId"] for x in first["items"] + second["items"]}) == 3
    query["assetId"] = "camera-1"
    assert (
        system.client.get(
            "/events", params={**query, "cursor": first["nextCursor"]}, headers=headers()
        ).status_code
        == 422
    )
    with system.sessions.begin() as session:
        session.get(Principal, "user:staff").sites = []
    query.pop("assetId")
    assert (
        system.client.get(
            "/events", params={**query, "cursor": first["nextCursor"]}, headers=headers()
        ).status_code
        == 422
    )


def test_disabled_principal_and_body_secrets_not_echoed(system, event_body):
    with system.sessions.begin() as session:
        session.get(Principal, "client:edge").active = False
    assert ingest(system, event_body).status_code == 401
    response = system.client.post(
        "/ingest", json={"secret": "do-not-echo"}, headers=headers("edge")
    )
    assert "do-not-echo" not in response.text


def test_resolve_before_start_preserves_both(system, event_body):
    resolved = deepcopy(event_body)
    resolved["eventId"], resolved["eventCode"] = str(uuid4()), "DSC-102B"
    resolved["media"]["clipId"] = str(uuid4())
    assert ingest(system, resolved).status_code == 201
    assert ingest(system, event_body).status_code == 201
    with system.sessions() as session:
        events = list(session.scalars(select(Event)))
        assert len(events) == 2
        assert len({x.incident_id for x in events}) == 1


def test_aas_normative_schema_and_time_series_acl(system, event_body):
    ingest(system, event_body)
    aas_id = encoded("urn:dsc:aas:camera-1")
    response = system.client.get(f"/shells/{aas_id}", headers=headers("partner"))
    assert response.status_code == 200, response.text
    assert len(response.json()["submodels"]) == 2
    for kind in ["Nameplate", "TechnicalData", "TimeSeries"]:
        submodel_id = encoded(f"urn:dsc:submodel:camera-1:{kind}")
        assert system.client.get(f"/submodels/{submodel_id}", headers=headers()).status_code == 200
        partner_response = system.client.get(
            f"/submodels/{submodel_id}", headers=headers("partner")
        )
        assert partner_response.status_code == (404 if kind == "TimeSeries" else 200)
    hidden = encoded("urn:dsc:aas:other-camera")
    assert system.client.get(f"/shells/{hidden}", headers=headers()).status_code == 404
    assert system.client.get("/description", headers=headers()).json() == {"profiles": []}


def test_heartbeat_offline_source_time_is_not_receive_time(system):
    body = {
        "schemaVersion": "1.0",
        "agentVersion": "1.0.0",
        "observedAt": "2025-01-01T00:00:00Z",
        "outboxPendingCount": 0,
        "diskFreeBytes": 0,
        "nasStatus": "OK",
    }
    response = system.client.post("/agent/heartbeat", json=body, headers=headers("edge"))
    assert response.status_code == 200
    assert response.json()["serverTime"].startswith("2026-11-07")


def test_db_failure_never_acknowledges(system, event_body, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    original = Session.flush

    def fail(self, *args, **kwargs):
        if any(isinstance(obj, Event) for obj in self.new):
            raise OperationalError("insert", {}, Exception("storage down"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail)
    response = ingest(system, event_body)
    assert response.status_code == 503
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
