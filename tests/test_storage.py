"""Storage details: engine configuration and the UTC timestamp column type."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine
from sqlalchemy.dialects import sqlite

from ledgerline.db import create_db_engine
from ledgerline.models import UtcDateTime

DIALECT = sqlite.dialect()
NEW_YORK_WINTER = timezone(timedelta(hours=-5))


def test_postgres_urls_get_a_psycopg_engine() -> None:
    engine = create_db_engine("postgresql+psycopg://user:password@localhost:5432/ledgerline_test")
    try:
        assert (engine.dialect.name, engine.dialect.driver) == ("postgresql", "psycopg")
    finally:
        engine.dispose()


def test_sqlite_connections_enforce_foreign_keys(db: Engine) -> None:
    if db.dialect.name != "sqlite":
        pytest.skip("SQLite-specific connection setup")
    with db.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


class TestUtcDateTime:
    column = UtcDateTime()

    def test_stores_aware_values_in_utc(self) -> None:
        local = datetime(2026, 1, 1, 12, 0, tzinfo=NEW_YORK_WINTER)
        stored = self.column.process_bind_param(local, DIALECT)
        assert stored == datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
        assert stored is not None
        assert stored.tzinfo is UTC

    def test_refuses_naive_values(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001 - deliberately naive
        with pytest.raises(ValueError, match="naive"):
            self.column.process_bind_param(naive, DIALECT)

    def test_reads_naive_sqlite_values_as_utc(self) -> None:
        from_sqlite = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001 - what SQLite returns
        assert self.column.process_result_value(from_sqlite, DIALECT) == datetime(
            2026, 1, 1, 12, 0, tzinfo=UTC
        )

    def test_reads_aware_values_as_utc(self) -> None:
        local = datetime(2026, 1, 1, 12, 0, tzinfo=NEW_YORK_WINTER)
        result = self.column.process_result_value(local, DIALECT)
        assert result is not None
        assert result.tzinfo is UTC

    def test_passes_null_through(self) -> None:
        assert self.column.process_bind_param(None, DIALECT) is None
        assert self.column.process_result_value(None, DIALECT) is None
