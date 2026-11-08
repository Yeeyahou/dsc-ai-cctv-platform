import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from dsc.backend.db import Attempt, Event, FileJob
from dsc.shared.contracts import EventInput

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
    queue_url = system.access("edge").agent.queue_url
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
