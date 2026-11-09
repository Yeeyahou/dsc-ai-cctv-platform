import base64
import hashlib

import pytest
from sqlalchemy import select

from dsc.backend.db import Attempt, FileJob, Principal
from tests.conftest import headers
from tests.test_backend import ingest


def requested(system, event_body):
    assert ingest(system, event_body).status_code == 201
    response = system.client.post(
        f"/files/{event_body['eventId']}/requests", json={}, headers=headers()
    )
    assert response.status_code == 202, response.text
    return response.json()["requestId"]


def claim(system):
    return system.client.post("/edge-server/jobs/claim", json={}, headers=headers("edge"))


def upload(system, job, content=b"clip"):
    path = f"/edge-server/jobs/{job['requestId']}/upload-url"
    body = {
        "leaseToken": job["leaseToken"],
        "sizeBytes": len(content),
        "contentType": "video/mp4",
        "checksumSha256": base64.b64encode(hashlib.sha256(content).digest()).decode(),
    }
    response = system.client.post(path, json=body, headers=headers("edge"))
    assert response.status_code == 200, response.text
    with system.sessions() as session:
        attempt = session.scalar(
            select(Attempt).where(
                Attempt.request_id == job["requestId"], Attempt.number == job["attempt"]
            )
        )
        system.storage.objects[attempt.object_key] = content
    return body


def test_repeated_request_one_job_and_one_lease(system, event_body):
    job_id = requested(system, event_body)
    repeated = system.client.post(
        f"/files/{event_body['eventId']}/requests", json={}, headers=headers()
    )
    assert repeated.json()["requestId"] == job_id
    first = claim(system)
    assert first.status_code == 200
    assert claim(system).status_code == 204
    with system.sessions() as session:
        assert len(list(session.scalars(select(FileJob)))) == 1
        assert len(list(session.scalars(select(Attempt)))) == 1


def test_verified_upload_and_expiry(system, event_body):
    job_id = requested(system, event_body)
    job = claim(system).json()
    upload(system, job)
    complete = f"/edge-server/jobs/{job_id}/complete"
    token = {"leaseToken": job["leaseToken"]}
    assert system.client.post(complete, json=token, headers=headers("edge")).json() == {
        "status": "READY"
    }
    assert system.client.post(complete, json=token, headers=headers("edge")).status_code == 200
    status = system.client.get(f"/file-requests/{job_id}", headers=headers())
    assert status.status_code == 200
    assert status.json()["downloadUrl"].startswith("https://")
    assert (
        system.client.get(f"/file-requests/{job_id}", headers=headers("partial")).status_code == 404
    )
    system.clock.advance(86401)
    status = system.client.get(f"/file-requests/{job_id}", headers=headers())
    assert status.json()["status"] == "EXPIRED"
    assert "downloadUrl" not in status.json()
    system.platform.maintain()
    assert system.storage.objects == {}
    repeated = system.client.post(
        f"/files/{event_body['eventId']}/requests", json={}, headers=headers()
    )
    assert repeated.status_code == 202 and repeated.json()["requestId"] != job_id


def test_false_complete_and_checksum_mismatch_never_ready(system, event_body):
    job_id = requested(system, event_body)
    job = claim(system).json()
    token = {"leaseToken": job["leaseToken"]}
    complete = f"/edge-server/jobs/{job_id}/complete"
    assert system.client.post(complete, json=token, headers=headers("edge")).status_code == 409
    upload(system, job)
    system.storage.objects = {key: b"evil" for key in system.storage.objects}
    response = system.client.post(complete, json=token, headers=headers("edge"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPLOAD_VERIFICATION_FAILED"
    status = system.client.get(f"/file-requests/{job_id}", headers=headers()).json()
    assert status["status"] == "UPLOADING" and "downloadUrl" not in status


def test_lease_expiry_reassignment_fences_old_completion(system, event_body):
    job_id = requested(system, event_body)
    first = claim(system).json()
    system.clock.advance(301)
    system.platform.maintain()
    second = claim(system).json()
    assert second["attempt"] == 2 and second["leaseToken"] != first["leaseToken"]
    token = {"leaseToken": first["leaseToken"]}
    for suffix in ("lease", "complete"):
        assert (
            system.client.post(
                f"/edge-server/jobs/{job_id}/{suffix}", json=token, headers=headers("edge")
            ).status_code
            == 409
        )
    token = {"leaseToken": second["leaseToken"]}
    assert (
        system.client.post(
            f"/edge-server/jobs/{job_id}/lease", json=token, headers=headers("edge-other")
        ).status_code
        == 404
    )


def test_enqueue_gap_recovery(system, event_body):
    system.queue.fail_send = True
    job_id = requested(system, event_body)
    assert system.queue.messages == []
    system.queue.fail_send = False
    system.platform.maintain()
    assert claim(system).json()["requestId"] == job_id


@pytest.mark.parametrize(
    "state,status", [("RECORDING", 409), ("MISSING", 404), ("NOT_APPLICABLE", 404)]
)
def test_unavailable_source_has_no_success_link(system, event_body, state, status):
    event_body["media"]["status"] = state
    assert ingest(system, event_body).status_code == 201
    response = system.client.post(
        f"/files/{event_body['eventId']}/requests", json={}, headers=headers()
    )
    assert response.status_code == status
    assert "downloadUrl" not in response.json()


def test_get_never_creates_job_and_deadline(system, event_body):
    ingest(system, event_body)
    assert (
        system.client.get(f"/files/{event_body['eventId']}", headers=headers()).status_code == 404
    )
    requested_result = system.client.post(
        f"/files/{event_body['eventId']}/requests", json={}, headers=headers()
    )
    system.clock.advance(1801)
    system.platform.maintain()
    value = system.client.get(
        f"/file-requests/{requested_result.json()['requestId']}", headers=headers()
    ).json()
    assert value["status"] == "FAILED" and value["failureCode"] == "JOB_TIMEOUT"


def test_disabled_requester_not_dispatched(system, event_body):
    job_id = requested(system, event_body)
    with system.sessions.begin() as session:
        session.get(Principal, "user:staff").active = False
    assert claim(system).status_code == 204
    with system.sessions() as session:
        assert session.get(FileJob, job_id).status == "FAILED"


def test_revoked_asset_grants_block_claim(system, event_body):
    job_id = requested(system, event_body)
    with system.sessions.begin() as session:
        session.get(Principal, "user:staff").sites = []
    assert claim(system).status_code == 204
    with system.sessions() as session:
        assert session.get(FileJob, job_id).status == "FAILED"


def test_revoked_requester_blocks_completion(system, event_body):
    job_id = requested(system, event_body)
    job = claim(system).json()
    upload(system, job)
    with system.sessions.begin() as session:
        session.get(Principal, "user:staff").video = False
    response = system.client.post(
        f"/edge-server/jobs/{job_id}/complete",
        json={"leaseToken": job["leaseToken"]},
        headers=headers("edge"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REQUESTER_DISABLED"
