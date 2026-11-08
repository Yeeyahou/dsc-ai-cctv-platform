from alembic import context

from dsc.backend.aws import load_secrets
from dsc.backend.config import Settings
from dsc.backend.db import Base, database

target_metadata = Base.metadata
settings = load_secrets(Settings())

if context.is_offline_mode():
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine, _ = database(settings.database_url.get_secret_value())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
