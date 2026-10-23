"""Engine and session setup, including the SQLite settings the ledger relies on."""

from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# How long a SQLite connection waits for the write lock before giving up.
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def create_db_engine(database_url: str) -> Engine:
    """Create an engine for ``database_url`` with the per-backend settings ledgerline needs."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        engine = create_engine(url, connect_args={"timeout": SQLITE_BUSY_TIMEOUT_SECONDS})
        _configure_sqlite(engine)
        return engine
    # pre_ping replaces connections the server closed while they sat in the pool.
    return create_engine(url, pool_pre_ping=True)


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        # Hand transaction control to SQLAlchemy (see _on_begin) instead of the
        # sqlite3 module, which would otherwise issue its own deferred BEGIN.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        # SQLite ignores foreign keys unless asked, per connection.
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Connection) -> None:
        # BEGIN IMMEDIATE takes SQLite's write lock up front, so concurrent
        # writers queue (up to the busy timeout) instead of both reading and then
        # failing with "database is locked" when they try to upgrade to a write
        # lock. SQLite allows one writer at a time anyway; this makes it orderly.
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False lets callers read attributes of committed objects
    # without another round trip.
    return sessionmaker(engine, expire_on_commit=False)
