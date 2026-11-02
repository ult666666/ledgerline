"""Storage details: engine configuration, the UTC timestamp type and the hold constraints."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError

from ledgerline.db import create_db_engine
from ledgerline.domain import HoldStatus
from ledgerline.models import Hold, Transaction, UtcDateTime
from tests.support import FakeClock, Json, authorize, deposit, open_account, post

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


class TestHoldConstraints:
    """The CHECK and UNIQUE constraints stop a buggy code path from storing a broken hold."""

    @pytest.fixture
    def wallets(self, clocked_client: TestClient) -> tuple[Json, Json]:
        alice = open_account(clocked_client, name="Alice", account_type="liability")
        bob = open_account(clocked_client, name="Bob", account_type="liability")
        deposit(clocked_client, open_account(clocked_client, name="Cash"), alice, 1_000)
        return alice, bob

    @staticmethod
    def valid_row(wallets: tuple[Json, Json], clock: FakeClock) -> dict[str, Any]:
        alice, bob = wallets
        return {
            "id": uuid4(),
            "from_account_id": UUID(alice["id"]),
            "to_account_id": UUID(bob["id"]),
            "amount": 100,
            "currency": "USD",
            "status": HoldStatus.ACTIVE,
            "captured_amount": None,
            "metadata": {},
            "created_at": clock.now,
            "expires_at": clock.now + timedelta(hours=1),
            "closed_at": None,
        }

    def test_a_valid_row_is_accepted(
        self, db: Engine, clock: FakeClock, wallets: tuple[Json, Json]
    ) -> None:
        with db.begin() as connection:
            connection.execute(insert(Hold).values(self.valid_row(wallets, clock)))

    @pytest.mark.parametrize(
        "changes",
        [
            {"status": "expired"},  # expiry is derived, never stored
            {"amount": 0},
            {"status": "captured", "closed_at": "now"},  # no captured amount
            {"status": "captured", "captured_amount": 101, "closed_at": "now"},
            {"status": "captured", "captured_amount": 0, "closed_at": "now"},
            {"status": "captured", "captured_amount": 50},  # no closing time
            {"status": "voided"},  # no closing time
            {"status": "voided", "captured_amount": 50, "closed_at": "now"},
            {"captured_amount": 50},  # an active hold with a captured amount
            {"to_account_id": "same"},
        ],
    )
    def test_invalid_rows_are_refused(
        self, db: Engine, clock: FakeClock, wallets: tuple[Json, Json], changes: dict[str, Any]
    ) -> None:
        row = self.valid_row(wallets, clock)
        resolved = {
            "now": clock.now,
            "same": row["from_account_id"],
        }
        row.update({k: resolved.get(v, v) if isinstance(v, str) else v for k, v in changes.items()})
        with pytest.raises(IntegrityError), db.begin() as connection:
            connection.execute(insert(Hold).values(row))

    def test_a_hold_reaches_the_journal_at_most_once(
        self, db: Engine, clocked_client: TestClient, clock: FakeClock, wallets: tuple[Json, Json]
    ) -> None:
        alice, bob = wallets
        hold = authorize(clocked_client, alice, bob, 100, expires_at=clock.now + timedelta(hours=1))
        captured = post(clocked_client, f"/v1/holds/{hold['id']}/capture", None).json()
        other = post(
            clocked_client,
            "/v1/transfers",
            {
                "from_account_id": alice["id"],
                "to_account_id": bob["id"],
                "amount": 1,
                "currency": "USD",
            },
        ).json()
        assert captured["capture_transaction_id"] != other["id"]
        with pytest.raises(IntegrityError), db.begin() as connection:
            connection.execute(
                update(Transaction)
                .where(Transaction.id == UUID(other["id"]))
                .values(hold_id=UUID(hold["id"]))
            )
