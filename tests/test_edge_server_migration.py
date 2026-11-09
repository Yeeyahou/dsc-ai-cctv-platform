import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from dsc.backend.db import database


@pytest.mark.parametrize(
    "postgres",
    [
        False,
        pytest.param(
            True,
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not os.environ.get("DSC_TEST_POSTGRES_URL"),
                    reason="requires disposable PostgreSQL database",
                ),
            ],
        ),
    ],
)
def test_edge_server_migration_preserves_installed_data(tmp_path, postgres):
    url = (
        os.environ["DSC_TEST_POSTGRES_URL"]
        if postgres
        else f"sqlite:///{tmp_path}/migration.sqlite"
    )
    engine, _ = database(url)
    with engine.connect() as conn:
        if postgres:
            schema = f"dsc_migration_{uuid4().hex}"
            conn.exec_driver_sql(f"CREATE SCHEMA {schema}")
            conn.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        command.upgrade(config, "7c195c8eabb1")

        def table(name):
            return sa.Table(name, sa.MetaData(), autoload_with=conn)

        def read(name):
            return conn.execute(sa.select(table(name))).mappings().one()

        principal = "client:installed"
        installed_id = "agent-installed"
        event_id, request_id, clip_id = (str(uuid4()) for _ in range(3))
        old_queue = "https://sqs.ap-northeast-2.amazonaws.com/example/old-queue"
        heartbeat = {"schemaVersion": "1.0", "agentVersion": "1.0.0", "outboxPendingCount": 7}
        scopes = ["dsc/ingest.write", "dsc/agent.work"]
        conn.execute(
            table("principals")
            .insert()
            .values(
                id=principal,
                active=True,
                scopes=scopes,
                sites=[],
                assets=[],
                submodels=[],
                video=False,
            )
        )
        conn.execute(
            table("agents")
            .insert()
            .values(
                id=installed_id,
                principal_id=principal,
                site_id="site-1",
                assets=[],
                queue_url=old_queue,
                heartbeat=heartbeat,
            )
        )
        conn.execute(
            table("events")
            .insert()
            .values(
                id=event_id,
                agent_id=installed_id,
                site_id="site-1",
                event_code="DSC-101",
                occurred_at=1000,
                received_at=2000,
                payload={"schemaVersion": "1.0", "eventId": event_id},
                payload_hash="test-hash",
            )
        )
        conn.execute(
            table("file_requests")
            .insert()
            .values(
                id=request_id,
                event_id=event_id,
                clip_id=clip_id,
                agent_id=installed_id,
                requested_by=principal,
                status="READY",
                created_at=1000,
                deadline=2000,
                attempt=1,
                dispatch_version=1,
                dispatched_version=1,
                retryable=False,
            )
        )
        command.upgrade(config, "head")

        assert "agents" not in sa.inspect(conn).get_table_names()
        edge_server = read("edge_servers")
        assert edge_server["id"] == installed_id
        assert edge_server["queue_url"] == old_queue
        assert edge_server["heartbeat"] == {
            "schemaVersion": "1.1",
            "edgeServerVersion": "1.0.0",
            "outboxPendingCount": 7,
        }
        assert read("principals")["scopes"] == ["dsc/ingest.write", "dsc/edge-server.work"]
        assert read("events")["edge_server_id"] == installed_id
        assert read("events")["payload_hash"] == "test-hash"
        assert read("file_requests")["edge_server_id"] == installed_id
        assert read("file_requests")["id"] == request_id
        assert read("file_requests")["status"] == "READY"
        assert any(
            f["referred_table"] == "edge_servers" and f["constrained_columns"] == ["edge_server_id"]
            for f in sa.inspect(conn).get_foreign_keys("file_requests")
        )

        command.downgrade(config, "7c195c8eabb1")
        assert read("agents")["heartbeat"] == heartbeat
        assert read("principals")["scopes"] == scopes
        assert read("file_requests")["agent_id"] == installed_id
    engine.dispose()
