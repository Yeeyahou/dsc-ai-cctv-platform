import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dsc.backend.admin import Registration, register
from dsc.backend.api import create_app
from dsc.backend.auth import authenticate
from dsc.backend.db import ArchiveTransfer, Attempt, ClipReplica, EdgeServer, FileJob
from dsc.backend.errors import Problem
from dsc.edge.archive import create_receiver
from dsc.edge.config import EdgeSettings
from dsc.edge.files import ImmutableWriter, stage_clip
from dsc.edge.inbox import receive_inbox
from dsc.edge.replication import replicate_pending
from dsc.edge.store import EdgeStore
from dsc.shared.contracts import (
    EventEnvelope,
    EventInput,
    MediaUpdate,
    ReplicaBody,
)
from tests.conftest import headers


def checksum(content=b"clip"):
    return base64.b64encode(hashlib.sha256(content).digest()).decode()


@pytest.fixture
def office(system, event_body, tmp_path):
    scopes = ["dsc/ingest.write", "dsc/edge-server.work", "dsc/timeseries.read"]
    register(
        system.sessions,
        Registration.model_validate(
            {
                "principals": [
                    {"principalId": "client:office", "scopes": ["dsc/edge-server.work"]},
                    {"principalId": "client:robot", "scopes": scopes[:2]},
                ],
                "edgeServers": [
                    {
                        "edgeServerId": "office",
                        "principalId": "client:office",
                        "siteId": "site-1",
                        "assets": ["camera-1", "rack-1"],
                        "queueUrl": "https://sqs.test/office",
                        "role": "archive",
                        "archiveUrl": "https://office.test",
                    },
                    {
                        "edgeServerId": "robot",
                        "principalId": "client:robot",
                        "siteId": "site-1",
                        "assets": ["camera-1", "rack-1"],
                        "queueUrl": "https://sqs.test/robot",
                        "role": "field",
                        "archiveServerId": "office",
                    },
                ],
            }
        ),
    )
    with system.sessions.begin() as session:
        source = session.get(EdgeServer, "edge-server-1")
        source.role, source.archive_server_id = "field", "office"

    def claims(token):
        return {
            "sub": token,
            "client_id": token,
            "scope": " ".join(scopes),
            **({"username": token} if token == "staff" else {}),
        }

    def access(token):
        with system.sessions() as session:
            return authenticate(session, claims(token))

    backend = TestClient(create_app(system.platform, claims), raise_server_exceptions=False)

    class Client:
        def __init__(self, token):
            self.token = token

        def post(self, path, body):
            response = backend.post(path, json=body, headers=headers(self.token))
            response.raise_for_status()
            return response.json() if response.status_code != 204 else None

    root = tmp_path / "office-nas"
    root.mkdir()
    (root / ".dsc-volume-id").write_text("synthetic-volume")
    producer = tmp_path / "producer"
    producer.mkdir()
    secret = tmp_path / "client-secret"
    secret.write_text("test-only")
    secret.chmod(0o600)
    common = dict(
        environment="test",
        site_id="site-1",
        api_url="https://api.test",
        token_url="https://auth.test/token",
        client_id="unused",
        secret_file=secret,
    )
    settings = EdgeSettings(
        **common,
        server_id="office",
        role="archive",
        nas_root=root,
        state_dir=tmp_path / "office-state",
        archive_volume_id="synthetic-volume",
    )
    local = EdgeSettings(
        **common,
        server_id="edge-server-1",
        role="field",
        nas_root=producer,
        state_dir=tmp_path / "field-state",
        inbox=tmp_path / "inbox",
        archive_allowed_hosts=["office.test"],
    )
    office_store = EdgeStore(settings.state_dir / "outbox.sqlite", clock=system.clock)
    field_store = EdgeStore(local.state_dir / "outbox.sqlite", clock=system.clock)
    receiver = TestClient(
        create_receiver(settings, Client("office"), office_store), raise_server_exceptions=False
    )
    source = Client("edge")

    def transport(request):
        response = receiver.request(
            request.method, request.url.path, content=request.read(), headers=request.headers
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    source.http = httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False)
    system.platform.ingest(access("edge"), EventInput.model_validate(event_body))
    for key in ("office", "edge-server-1", "robot"):
        with system.sessions.begin() as session:
            server = session.get(EdgeServer, key)
            server.last_seen = system.clock()
            server.heartbeat = {"nasStatus": "OK"}
    body = ReplicaBody(
        eventId=event_body["eventId"],
        clipId=event_body["media"]["clipId"],
        revision=0,
        sizeBytes=4,
        checksumSha256=checksum(),
    )
    yield type(
        "Office",
        (),
        {
            "settings": settings,
            "local": local,
            "source": source,
            "receiver": receiver,
            "backend": backend,
            "access": staticmethod(access),
            "body": body,
            "store": office_store,
            "field_store": field_store,
        },
    )()
    source.http.close()
    receiver.close()
    backend.close()


def stage(office, event_body):
    (office.local.nas_root / "producer.mp4").write_bytes(b"clip")
    envelope = EventEnvelope(
        kind="event",
        event=event_body,
        localMedia={"clipId": event_body["media"]["clipId"], "relativePath": "producer.mp4"},
    )
    staged = stage_clip(office.local, event_body["eventId"], envelope.event.media, "producer.mp4")
    office.field_store.accept(envelope, staged_copy=staged)
    office.field_store.mark(office.field_store.next_item()["id"], "ACKED")
    return staged


def transfer(system, office, data=b"clip"):
    ticket = system.platform.replicas.prepare(office.access("edge"), office.body)
    response = office.receiver.put(
        f"/archive/transfers/{ticket['transferId']}",
        content=data,
        headers={"Authorization": f"Bearer {ticket['transferToken']}", "Content-Type": "video/mp4"},
    )
    return ticket, response


def request(system, office):
    return system.platform.request_clip(office.access("staff"), office.body.wire()["eventId"])[0][
        "requestId"
    ]


def test_complete_replication_and_primary_assignment(system, office, event_body):
    staged = stage(office, event_body)
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "STORED"
    assert (office.settings.nas_root / staged["relative_path"]).read_bytes() == b"clip"
    request(system, office)
    assert system.platform.claim(office.access("edge")) is None
    job = system.platform.claim(office.access("office"))
    assert job["checksumSha256"] == checksum() and job["revision"] == 0
    assert job["sizeBytes"] == 4


def test_partial_wrong_and_oversized_bytes_never_confirm(system, office):
    for content in (b"cli", b"evil", b"clipx"):
        ticket, response = transfer(system, office, content)
        assert response.status_code == 409
        with system.sessions() as session:
            assert session.get(ArchiveTransfer, ticket["transferId"]).completed_at is None
            assert session.get(ClipReplica, (str(office.body.event_id), 0, "office")) is None
    assert office.store.copies() == []


def test_retry_after_lost_ack_and_restart_preserves_identity(
    system, office, event_body, monkeypatch
):
    stage(office, event_body)
    original = office.source.http

    class DropAcknowledgement:
        def post(self, *args, **kwargs):
            return original.post(*args, **kwargs)

        def put(self, *args, **kwargs):
            response = original.put(*args, **kwargs)
            assert response.status_code == 200
            raise httpx.ReadError("simulated lost response")

    office.source.http = DropAcknowledgement()
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "LOCAL"
    system.clock.advance(61)
    office.source.http = original
    recovered = EdgeStore(office.local.state_dir / "outbox.sqlite", clock=system.clock)
    replicate_pending(office.source, recovered, office.local)
    assert recovered.copy(event_body["eventId"], 0)["state"] == "STORED"
    with system.sessions() as session:
        assert len(list(session.scalars(select(ClipReplica)))) == 2


def test_crash_between_file_commit_and_manifest_is_recovered(system, office, monkeypatch):
    original = office.store.save_copy

    def unavailable(*args, **kwargs):
        raise OSError("simulated disk or process interruption")

    monkeypatch.setattr(office.store, "save_copy", unavailable)
    ticket, response = transfer(system, office)
    assert response.status_code == 503
    monkeypatch.setattr(office.store, "save_copy", original)
    _, repeated = transfer(system, office)
    assert repeated.status_code == 200
    assert len(office.store.copies()) == 1


def test_file_commit_survives_backend_confirmation_failure(system, office, monkeypatch):
    replicas = system.platform.replicas
    original = replicas.complete

    def fail(*_):
        raise Problem(503, "DATABASE_UNAVAILABLE", "unavailable", True)

    monkeypatch.setattr(replicas, "complete", fail)
    _, response = transfer(system, office)
    assert response.status_code == 503
    monkeypatch.setattr(replicas, "complete", original)
    _, response = transfer(system, office)
    assert response.status_code == 200


@pytest.mark.parametrize("failure", ["offline", "nas", "unclaimed"])
def test_queued_primary_failure_routes_to_field(system, office, failure):
    assert transfer(system, office)[1].status_code == 200
    job_id = request(system, office)
    with system.sessions.begin() as session:
        primary = session.get(EdgeServer, "office")
        if failure == "offline":
            primary.last_seen = system.clock() - 181000
        elif failure == "nas":
            primary.heartbeat = {"nasStatus": "UNREACHABLE"}
    if failure == "unclaimed":
        system.clock.advance(61)
    system.platform.maintain()
    assert system.platform.claim(office.access("office")) is None
    assert system.platform.claim(office.access("edge"))["requestId"] == job_id


def test_active_lease_fences_failover_until_expiry(system, office):
    transfer(system, office)
    job_id = request(system, office)
    primary = system.platform.claim(office.access("office"))
    with system.sessions.begin() as session:
        session.get(EdgeServer, "office").heartbeat = {"nasStatus": "UNREACHABLE"}
    system.platform.maintain()
    assert system.platform.claim(office.access("edge")) is None
    system.clock.advance(301)
    with system.sessions.begin() as session:
        session.get(EdgeServer, "edge-server-1").last_seen = system.clock()
    system.platform.maintain()
    backup = system.platform.claim(office.access("edge"))
    assert backup["attempt"] == 2
    assert backup["leaseToken"] != primary["leaseToken"]
    for action in ("complete", "lease", "fail"):
        body = {"leaseToken": primary["leaseToken"]}
        if action == "fail":
            body.update(failureCode="UPLOAD_FAILED", retryable=True)
        response = office.backend.post(
            f"/edge-server/jobs/{job_id}/{action}", json=body, headers=headers("office")
        )
        assert response.status_code in {404, 409}


def test_pinned_hash_and_revoked_uploader_cannot_complete(system, office):
    transfer(system, office)
    job_id = request(system, office)
    job = system.platform.claim(office.access("office"))
    path = f"/edge-server/jobs/{job_id}/upload-url"
    response = office.backend.post(
        path,
        headers=headers("office"),
        json={
            "leaseToken": job["leaseToken"],
            "sizeBytes": 4,
            "contentType": "video/mp4",
            "checksumSha256": checksum(b"evil"),
        },
    )
    assert response.status_code == 409
    with system.sessions.begin() as session:
        session.get(EdgeServer, "office").assets = ["camera-1"]
    response = office.backend.post(
        f"/edge-server/jobs/{job_id}/lease",
        headers=headers("office"),
        json={"leaseToken": job["leaseToken"]},
    )
    assert response.status_code == 404


def test_other_site_and_other_field_cannot_register_or_confirm(system, office):
    for token in ("edge-other", "robot"):
        response = office.backend.post(
            "/edge-server/replicas", json=office.body.wire(), headers=headers(token)
        )
        assert response.status_code == 404
    ticket, _ = transfer(system, office)
    receipt = {
        "transferToken": ticket["transferToken"],
        "sizeBytes": 4,
        "checksumSha256": checksum(),
    }
    response = office.backend.post(
        f"/edge-server/archive/{ticket['transferId']}/complete",
        json=receipt,
        headers=headers("edge"),
    )
    assert response.status_code == 404


def test_same_revision_changed_hash_and_expired_ticket_are_rejected(system, office):
    ticket, _ = transfer(system, office)
    response = office.backend.post(
        "/edge-server/replicas",
        headers=headers("edge"),
        json={**office.body.wire(), "checksumSha256": checksum(b"evil")},
    )
    assert response.status_code == 409
    system.clock.advance(901)
    response = office.receiver.put(
        f"/archive/transfers/{ticket['transferId']}",
        content=b"clip",
        headers={"Authorization": f"Bearer {ticket['transferToken']}", "Content-Type": "video/mp4"},
    )
    assert response.status_code == 409


def test_volume_missing_cannot_confirm_or_report_healthy(system, office):
    from dsc.edge.main import heartbeat

    (office.settings.nas_root / ".dsc-volume-id").unlink()
    _, response = transfer(system, office)
    assert response.status_code == 404
    assert heartbeat(office.settings, office.store)["nasStatus"] == "UNREACHABLE"


def test_cleanup_waits_for_nas_and_active_jobs(system, office, event_body):
    staged = stage(office, event_body)
    system.platform.replicas.register(office.access("edge"), office.body)
    system.clock.advance(31 * 86400)
    response = office.backend.post(
        "/edge-server/replicas/retire", json=office.body.wire(), headers=headers("edge")
    )
    assert response.status_code == 409
    assert transfer(system, office)[1].status_code == 200
    with system.sessions.begin() as session:
        for name in ("office", "edge-server-1"):
            session.get(EdgeServer, name).last_seen = system.clock()
    request(system, office)
    response = office.backend.post(
        "/edge-server/replicas/retire", json=office.body.wire(), headers=headers("edge")
    )
    assert response.status_code == 409
    system.clock.advance(1801)
    system.platform.maintain()
    with system.sessions.begin() as session:
        session.get(EdgeServer, "office").last_seen = system.clock()
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "DELETED"
    assert not (office.local.cache_root / staged["relative_path"]).exists()
    assert (office.local.nas_root / "producer.mp4").read_bytes() == b"clip"
    assert office.field_store.rows()[0]["state"] == "ACKED"


def test_cleanup_crash_after_server_retirement_resumes(system, office, event_body, monkeypatch):
    import dsc.edge.replication as replication

    stage(office, event_body)
    system.clock.advance(31 * 86400)
    with system.sessions.begin() as session:
        session.get(EdgeServer, "office").last_seen = system.clock()
    original = replication.delete_copy

    def fail(*args):
        raise OSError("simulated process stop")

    monkeypatch.setattr(replication, "delete_copy", fail)
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "RETIRING"
    with system.sessions() as session:
        assert not session.get(ClipReplica, (event_body["eventId"], 0, "edge-server-1")).present
    monkeypatch.setattr(replication, "delete_copy", original)
    system.clock.advance(61)
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "DELETED"


def test_field_acceptance_stages_durable_clip_before_receipt(system, office, event_body):
    (office.local.nas_root / "producer.mp4").write_bytes(b"clip")
    office.local.inbox.mkdir()
    path = office.local.inbox / f"{event_body['eventId']}.event.json"
    path.write_text(
        json.dumps(
            {
                "kind": "event",
                "event": event_body,
                "localMedia": {
                    "clipId": event_body["media"]["clipId"],
                    "relativePath": "producer.mp4",
                },
            }
        )
    )
    assert receive_inbox(office.local, office.field_store) == 1
    copy = office.field_store.copy(event_body["eventId"], 0)
    assert (office.local.cache_root / copy["relative_path"]).read_bytes() == b"clip"
    assert copy["checksum"] == checksum()


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DSC_TEST_POSTGRES_URL"), reason="disposable PostgreSQL required"
)
def test_primary_and_fallback_simultaneous_claim_one_valid_lease(system, office):
    transfer(system, office)
    job_id = request(system, office)
    system.clock.advance(61)
    system.platform.maintain()
    barrier = Barrier(6)

    def claim(index):
        actor = office.access("office" if index % 2 else "edge")
        barrier.wait(timeout=10)
        return system.platform.claim(actor)

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(claim, range(6)))
    assert len([job for job in jobs if job]) == 1
    with system.sessions() as session:
        assert len(list(session.scalars(select(Attempt).where(Attempt.request_id == job_id)))) == 1


def test_retirement_ack_loss_is_reconciled_without_reupload(system, office, event_body):
    stage(office, event_body)
    system.clock.advance(31 * 86400)
    with system.sessions.begin() as session:
        session.get(EdgeServer, "office").last_seen = system.clock()
    original = office.source.post
    dropped = False

    def post(path, body):
        nonlocal dropped
        result = original(path, body)
        if path.endswith("/retire") and not dropped:
            dropped = True
            raise httpx.ReadError("simulated retirement ACK loss")
        return result

    office.source.post = post
    replicate_pending(office.source, office.field_store, office.local)
    system.clock.advance(61)
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "DELETED"


def test_changed_revision_fences_existing_upload_and_preserves_pinned_bytes(
    system, office, event_body
):
    transfer(system, office)
    job_id = request(system, office)
    job = system.platform.claim(office.access("office"))
    update = MediaUpdate.model_validate({**event_body["media"], "revision": 1})
    system.platform.media(office.access("edge"), event_body["eventId"], update)
    response = office.backend.post(
        f"/edge-server/jobs/{job_id}/complete",
        headers=headers("office"),
        json={"leaseToken": job["leaseToken"]},
    )
    assert response.status_code == 409
    response = office.backend.post(
        "/edge-server/replicas",
        headers=headers("edge"),
        json={**office.body.wire(), "revision": 1, "checksumSha256": checksum(b"evil")},
    )
    assert response.status_code == 409


def test_robot_event_is_assigned_to_same_office_without_cctv_identity(system, office, event_body):
    body = {
        **event_body,
        "eventId": str(uuid4()),
        "media": {**event_body["media"], "clipId": str(uuid4())},
    }
    system.platform.ingest(office.access("robot"), EventInput.model_validate(body))
    replica = ReplicaBody(
        eventId=body["eventId"],
        clipId=body["media"]["clipId"],
        revision=0,
        sizeBytes=4,
        checksumSha256=checksum(),
    )
    ticket = system.platform.replicas.prepare(office.access("robot"), replica)
    response = office.receiver.put(
        f"/archive/transfers/{ticket['transferId']}",
        content=b"clip",
        headers={"Authorization": f"Bearer {ticket['transferToken']}", "Content-Type": "video/mp4"},
    )
    assert response.status_code == 200
    system.platform.request_clip(office.access("staff"), body["eventId"])
    assert system.platform.claim(office.access("office"))["eventId"] == body["eventId"]
    with system.sessions() as session:
        assert session.get(ClipReplica, (body["eventId"], 0, "robot")) is not None
        assert session.get(ClipReplica, (body["eventId"], 0, "edge-server-1")) is None


def test_incomplete_clip_does_not_block_other_event_acceptance(system, office, event_body):
    office.local.inbox.mkdir()
    missing = office.local.inbox / f"{event_body['eventId']}.event.json"
    missing.write_text(
        json.dumps(
            {
                "kind": "event",
                "event": event_body,
                "localMedia": {
                    "clipId": event_body["media"]["clipId"],
                    "relativePath": "missing.mp4",
                },
            }
        )
    )
    another = {**event_body, "eventId": str(uuid4())}
    path = office.local.inbox / f"{another['eventId']}.event.json"
    path.write_text(json.dumps({"kind": "event", "event": another}))
    assert receive_inbox(office.local, office.field_store) == 1
    assert missing.exists()
    assert office.field_store.rows()[0]["event_id"] == another["eventId"]


def test_legacy_outbox_is_staged_on_field_role_upgrade(system, office, event_body):
    from dsc.edge.replication import recover_unstaged

    (office.local.nas_root / "legacy.mp4").write_bytes(b"clip")
    office.field_store.accept(
        EventEnvelope(
            kind="event",
            event=event_body,
            localMedia={"clipId": event_body["media"]["clipId"], "relativePath": "legacy.mp4"},
        )
    )
    recover_unstaged(office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["checksum"] == checksum()


def test_corrupt_managed_copy_is_quarantined_before_any_remote_upload(system, office, event_body):
    copy = stage(office, event_body)
    (office.local.cache_root / copy["relative_path"]).write_bytes(b"evil")
    replicate_pending(office.source, office.field_store, office.local)
    assert office.field_store.copy(event_body["eventId"], 0)["state"] == "QUARANTINED"
    assert office.field_store.copies(ready_only=True) == []
    assert office.store.copies() == []


def test_office_symlink_and_wrong_volume_cannot_acknowledge(system, office, tmp_path):
    root = office.settings.nas_root
    (root / str(office.body.event_id)).symlink_to(tmp_path, target_is_directory=True)
    assert transfer(system, office)[1].status_code == 503
    (root / ".dsc-volume-id").write_text("different-volume")
    assert transfer(system, office)[1].status_code == 503


def test_partial_crash_file_is_cleaned_only_under_same_clip_lock(tmp_path):
    root = tmp_path / "clips"
    root.mkdir()
    (root / ".clip.mp4.old.partial").write_bytes(b"cl")
    with ImmutableWriter(root, "clip.mp4", 4, checksum()) as writer:
        writer.write(b"clip")
        writer.finish()
    assert not list(root.glob("*.partial"))
    assert (root / "clip.mp4").read_bytes() == b"clip"


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DSC_TEST_POSTGRES_URL"), reason="disposable PostgreSQL required"
)
def test_retirement_racing_request_preserves_an_available_copy(system, office):
    transfer(system, office)
    system.clock.advance(31 * 86400)
    with system.sessions.begin() as session:
        for name in ("office", "edge-server-1"):
            session.get(EdgeServer, name).last_seen = system.clock()
    barrier = Barrier(2)

    def retire():
        actor = office.access("edge")
        barrier.wait(timeout=10)
        try:
            return system.platform.replicas.retire(actor, office.body)["status"]
        except Problem as exc:
            assert exc.code == "REPLICA_IN_USE"
            return "PRESERVED"

    def create():
        actor = office.access("staff")
        barrier.wait(timeout=10)
        return system.platform.request_clip(actor, str(office.body.event_id))[0]["requestId"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        retired, created = pool.submit(retire), pool.submit(create)
        result, job_id = retired.result(), created.result()
    assert result in {"RETIRED", "PRESERVED"}
    with system.sessions() as session:
        assert session.get(FileJob, job_id).edge_server_id == "office"
        assert session.get(ClipReplica, (str(office.body.event_id), 0, "office")).present


def test_historical_verified_copy_survives_later_media_status(system, office, event_body):
    transfer(system, office)
    system.platform.media(
        office.access("edge"),
        event_body["eventId"],
        MediaUpdate.model_validate(
            {"clipId": event_body["media"]["clipId"], "revision": 1, "status": "MISSING"}
        ),
    )
    assert (
        system.platform.replicas.register(office.access("edge"), office.body)["status"] == "PRESENT"
    )
    changed = office.body.model_copy(update={"checksum_sha256": checksum(b"evil")})
    with pytest.raises(Problem) as error:
        system.platform.replicas.register(office.access("edge"), changed)
    assert error.value.code == "REPLICA_CONTENT_CONFLICT"


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DSC_TEST_POSTGRES_URL"), reason="disposable PostgreSQL required"
)
def test_replication_and_office_upload_with_restricted_runtime_grants(system, office):
    from pathlib import Path

    from sqlalchemy.orm import sessionmaker

    from dsc.backend.service import Platform
    from dsc.shared.contracts import TransferReceipt

    actors = {name: office.access(name) for name in ("edge", "office", "staff")}
    with system.engine.connect() as connection:
        role = "dsc_replication_grants_test"
        connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for statement in (
            Path("deploy/database/grants.sql").read_text().replace("dsc_runtime", role).split(";")
        ):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")
        sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        service = Platform(
            sessions, system.platform.settings, system.storage, system.queue, system.clock
        )
        assert service.replicas.register(actors["edge"], office.body)["status"] == "PRESENT"
        ticket = service.replicas.prepare(actors["edge"], office.body)
        receipt = TransferReceipt(
            transferToken=ticket["transferToken"], sizeBytes=4, checksumSha256=checksum()
        )
        assert (
            service.replicas.complete(actors["office"], ticket["transferId"], receipt)["status"]
            == "STORED"
        )
        job_id = service.request_clip(actors["staff"], str(office.body.event_id))[0]["requestId"]
        assert service.claim(actors["office"])["requestId"] == job_id
