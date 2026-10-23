"""Shared fixtures.

Tests use a throwaway SQLite file by default. Set DATABASE_URL to run them
against PostgreSQL instead. Every test deletes all rows, so the database name
must contain "test" - a guard against pointing the suite at real data.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from ledgerline.config import Settings
from ledgerline.db import create_db_engine, create_session_factory
from ledgerline.models import Base
from ledgerline.service import LedgerService


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    configured = os.environ.get("DATABASE_URL")
    if not configured:
        return f"sqlite:///{tmp_path_factory.mktemp('db') / 'ledgerline_test.db'}"
    # Settings normalises postgres:// URLs to the psycopg driver.
    url = Settings(database_url=configured, _env_file=None).database_url
    database = make_url(url).database or ""
    if "test" not in database:
        pytest.exit(
            f"refusing to run: the tests delete all rows and DATABASE_URL names {database!r}; "
            "use a database whose name contains 'test'",
            returncode=2,
        )
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_db_engine(database_url)
    # Builds the schema locally. In CI's PostgreSQL job the schema already comes
    # from `alembic upgrade head`, and create_all leaves existing tables alone.
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def reset_database(engine: Engine) -> None:
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture
def db(engine: Engine) -> Engine:
    """The shared engine, with every table emptied before the test."""
    reset_database(engine)
    return engine


@pytest.fixture
def service(db: Engine) -> LedgerService:
    return LedgerService(create_session_factory(db))
