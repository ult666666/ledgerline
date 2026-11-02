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
    sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode recreates tables.
        render_as_batch=sqlite,
        # db.py starts every SQLite transaction with an explicit BEGIN IMMEDIATE,
        # so DDL is transactional there too: run all migrations in one
        # transaction, which a failed foreign-key check can still roll back.
        transactional_ddl=True,
    )


def _set_sqlite_foreign_keys(connection: Connection, *, enabled: bool) -> None:
    # The pragma is a no-op inside a transaction, so set it on the driver
    # connection, which db.py leaves in autocommit mode between transactions.
    cursor = connection.connection.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys = {'ON' if enabled else 'OFF'}")
    finally:
        cursor.close()


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
            sqlite = connection.dialect.name == "sqlite"
            if sqlite:
                # A batch migration rebuilds a SQLite table: copy it, drop the old
                # one, rename the copy. With foreign keys enforced, dropping a table
                # that other rows reference fails, so follow SQLite's documented
                # procedure: turn enforcement off for the migration and check
                # every reference before committing.
                # https://www.sqlite.org/lang_altertable.html#otheralter
                _set_sqlite_foreign_keys(connection, enabled=False)
            try:
                _configure(connection)
                with context.begin_transaction():
                    context.run_migrations()
                    if sqlite:
                        _check_sqlite_foreign_keys(connection)
            finally:
                if sqlite:
                    _set_sqlite_foreign_keys(connection, enabled=True)
    finally:
        engine.dispose()


def _check_sqlite_foreign_keys(connection: Connection) -> None:
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(
            f"migration left {len(violations)} broken foreign key reference(s), "
            f"first: {tuple(violations[0])}; rolled back"
        )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
