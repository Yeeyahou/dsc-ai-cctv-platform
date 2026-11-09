import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from dsc.backend.db import Attempt, Audit, Event, FileJob
from dsc.shared.contracts import EventInput, MediaUpdate

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DSC_TEST_POSTGRES_URL"),
        reason="requires disposable PostgreSQL database",
    ),
]


def test_concurrent_ingest_unique_event(system, event_body):
    barrier = Barrier(6)

    def send(_):
        actor = system.access("edge")
        barrier.wait(timeout=10)
        return system.platform.ingest(actor, EventInput.model_validate(event_body))[1]

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(send, range(6)))
    assert statuses.count(201) == 1
    assert statuses.count(200) == 5
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_concurrent_clip_requests_one_active_job(system, event_body):
    system.platform.ingest(system.access("edge"), EventInput.model_validate(event_body))
    barrier = Barrier(6)

    def request(_):
        actor = system.access("staff")
        barrier.wait(timeout=10)
        return system.platform.request_clip(actor, event_body["eventId"])[0]["requestId"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(request, range(6)))
    assert len(set(ids)) == 1
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(FileJob)) == 1


def test_duplicate_queue_messages_one_lease(system, event_body):
    system.platform.ingest(system.access("edge"), EventInput.model_validate(event_body))
    job_id = system.platform.request_clip(system.access("staff"), event_body["eventId"])[0][
        "requestId"
    ]
    queue_url = system.access("edge").edge_server.queue_url
    for _ in range(5):
        system.queue.send(queue_url, job_id, 1)
    barrier = Barrier(6)

    def claim(_):
        actor = system.access("edge")
        barrier.wait(timeout=10)
        return system.platform.claim(actor)

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(claim, range(6)))
    assert len([j for j in jobs if j]) == 1
    with system.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1


def test_event_read_and_clip_request_with_runtime_grants(system, event_body, monkeypatch):
    edge, staff = system.access("edge"), system.access("staff")
    event_id = event_body["eventId"]
    system.platform.ingest(edge, EventInput.model_validate(event_body))
    role = "dsc_postgres_runtime_grants_test"
    grants = Path("deploy/database/grants.sql").read_text().replace("dsc_runtime", role)
    # The outer transaction rolls back both the temporary role and its grants.
    with system.engine.connect() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for statement in grants.split(";"):
            if statement.strip():
                connection.exec_driver_sql(statement)
        connection.exec_driver_sql(f"SET LOCAL ROLE {role}")
        runtime_sessions = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        monkeypatch.setattr(system.platform, "sessions", runtime_sessions)
        assert system.platform.get_event(staff, event_id)["eventId"] == event_id
        media = MediaUpdate.model_validate({**event_body["media"], "revision": 1})
        assert system.platform.media(edge, event_id, media)["revision"] == 1
        job, status = system.platform.request_clip(staff, event_id)
        assert status == 202
        assert job["status"] == "QUEUED"
        connection.exec_driver_sql("RESET ROLE")
        actions = connection.scalars(select(Audit.action).where(Audit.object_id == event_id)).all()
        assert "event.read" in actions
