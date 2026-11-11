"""Temporary IAM-invoked initializer for an isolated DSC development database."""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

from dsc.backend.admin import Registration, register
from dsc.backend.aws import client, load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import database
from dsc.shared.observability import configure_logging, emit_event, logged

ROOT = Path(__file__).resolve().parent


@logged()
def secret_url(name: str) -> URL:
    settings = load_secrets(
        Settings(
            _env_file=None,
            database_secret_arn=os.environ[name],
            cursor_secret="database-init-does-not-sign-cursors",
        )
    )
    return make_url(settings.database_url.get_secret_value())


@logged()
def initialize(master: URL, runtime: URL, registration: Registration | None) -> None:
    if (
        master.host != runtime.host
        or master.port != runtime.port
        or master.database != "dsc"
        or runtime.database != "dsc"
        or master.username != "dsc_owner"
        or runtime.username != "dsc_runtime"
        or not runtime.password
    ):
        raise ValueError("unexpected development database connection settings")
    engine, _ = database(master.render_as_string(hide_password=False))
    with engine.begin() as conn:
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(54172601)")
        roles = set(
            conn.exec_driver_sql(
                "SELECT rolname FROM pg_roles WHERE rolname IN ('dsc_migrator', 'dsc_runtime')"
            ).scalars()
        )
        if "dsc_migrator" not in roles:
            conn.exec_driver_sql("CREATE ROLE dsc_migrator NOLOGIN")
        # Password literals are required by ALTER ROLE; quote with psycopg and never log the SQL.
        operation = "ALTER ROLE" if "dsc_runtime" in roles else "CREATE ROLE"
        statement = sql.SQL(operation + " dsc_runtime LOGIN PASSWORD {}").format(
            sql.Literal(runtime.password)
        )
        with conn.connection.cursor() as cursor:
            cursor.execute(statement)
        conn.exec_driver_sql("GRANT dsc_migrator TO CURRENT_USER")
        conn.exec_driver_sql("ALTER SCHEMA public OWNER TO dsc_migrator")
        conn.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.exec_driver_sql("GRANT CONNECT ON DATABASE dsc TO dsc_migrator, dsc_runtime")
        conn.exec_driver_sql("SET LOCAL ROLE dsc_migrator")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO dsc_runtime")
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.attributes["connection"] = conn
        command.upgrade(config, "head")
        for grant in (ROOT / "grants.sql").read_text().split(";"):
            if grant.strip():
                conn.exec_driver_sql(grant)
        if registration is not None:
            register(
                sessionmaker(bind=conn, join_transaction_mode="create_savepoint"), registration
            )

    runtime_engine, _ = database(runtime.render_as_string(hide_password=False))
    with runtime_engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1 FROM events LIMIT 1")


@logged()
def inspect_database(runtime: URL) -> dict:
    engine, _ = database(runtime.render_as_string(hide_password=False))
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            installation_table = "edge_servers" if "edge_servers" in tables else "agents"
            columns = "id,principal_id,site_id,assets,queue_url"
            if "role" in {
                column["name"] for column in inspect(conn).get_columns(installation_table)
            }:
                columns += ",role,archive_server_id,archive_url"
            installations = [
                dict(row)
                for row in conn.execute(
                    text(f"SELECT {columns} FROM {installation_table} ORDER BY id")
                ).mappings()
            ]
            counts = {
                table: conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in (
                    "events",
                    "event_assets",
                    "event_media",
                    "file_requests",
                    "job_leases",
                )
            }
            queries = {
                "events": "SELECT id,payload,payload_hash FROM events ORDER BY id",
                "event_assets": (
                    "SELECT event_id,asset_id,role FROM event_assets "
                    "ORDER BY event_id,asset_id,role"
                ),
                "event_media": (
                    "SELECT event_id,clip_id,revision,payload FROM event_media ORDER BY event_id"
                ),
            }
            content = {
                table: [dict(row) for row in conn.execute(text(query)).mappings()]
                for table, query in queries.items()
            }
            fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            return {
                "integrityDigest": fingerprint,
                "clientPrincipals": [
                    dict(row)
                    for row in conn.execute(
                        text(
                            "SELECT id,active,scopes,sites,assets,submodels,video FROM principals "
                            "WHERE id LIKE 'client:%' ORDER BY id"
                        )
                    ).mappings()
                ],
                "schemaRevision": conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
                "installations": installations,
                "counts": counts,
            }
    finally:
        engine.dispose()


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    configure_logging("dev-db-init")
    try:
        if set(event) - {"registration", "inspectOnly"}:
            raise ValueError("unexpected initializer arguments")
        data = (
            Registration.model_validate(event["registration"]) if "registration" in event else None
        )
        runtime = secret_url("RUNTIME_DB_SECRET_ARN")
        master_secret = client("secretsmanager", os.environ["AWS_REGION"]).get_secret_value(
            SecretId=os.environ["MASTER_SECRET_ARN"]
        )
        master = json.loads(master_secret["SecretString"])
        master_url = runtime.set(username=master["username"], password=master["password"])
        if event.get("inspectOnly") is True:
            return {"status": "inspected", **inspect_database(master_url)}
        initialize(master_url, runtime, data)
        return {"status": "initialized", **inspect_database(master_url)}
    except Exception as exc:
        # SQL exceptions can contain the ALTER ROLE statement and its password.
        emit_event("dev_db_init_failed", level=logging.ERROR, errorType=type(exc).__name__)
        raise RuntimeError(
            "Development database initialization failed; see structured logs"
        ) from None
