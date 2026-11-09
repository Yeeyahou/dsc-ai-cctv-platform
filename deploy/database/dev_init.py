"""Temporary IAM-invoked initializer for an isolated DSC development database."""

import json
import logging
import os
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from psycopg import sql
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


def handler(event: dict[str, Any], _context: Any) -> dict[str, str]:
    configure_logging("dev-db-init")
    try:
        if set(event) - {"registration"}:
            raise ValueError("unexpected initializer arguments")
        data = (
            Registration.model_validate(event["registration"]) if "registration" in event else None
        )
        runtime = secret_url("RUNTIME_DB_SECRET_ARN")
        master_secret = client("secretsmanager", os.environ["AWS_REGION"]).get_secret_value(
            SecretId=os.environ["MASTER_SECRET_ARN"]
        )
        master = json.loads(master_secret["SecretString"])
        initialize(
            runtime.set(username=master["username"], password=master["password"]), runtime, data
        )
    except Exception as exc:
        # SQL exceptions can contain the ALTER ROLE statement and its password.
        emit_event("dev_db_init_failed", level=logging.ERROR, errorType=type(exc).__name__)
        raise RuntimeError(
            "Development database initialization failed; see structured logs"
        ) from None
    return {"status": "initialized"}
