"""Alembic environment: runs migrations against the database in DATABASE_URL."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from ledgerline.config import Settings
from ledgerline.db import create_db_engine
from ledgerline.models import Base

config = context.config
target_metadata = Base.metadata

# Configure logging only when run from the alembic CLI (with an ini file), and
# without disabling loggers that already exist, e.g. when tests run migrations.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _database_url() -> str:
    # An explicit sqlalchemy.url (set programmatically, e.g. by tests) wins.
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode recreates tables.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine(_database_url())
    try:
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
