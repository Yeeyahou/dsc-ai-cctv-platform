from alembic import context

from dsc.backend.aws import load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import Base, database

target_metadata = Base.metadata


def run_online(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    settings = load_secrets(Settings())
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        run_online(supplied_connection)
    else:
        settings = load_secrets(Settings())
        engine, _ = database(settings.database_url.get_secret_value())
        with engine.connect() as connection:
            run_online(connection)
