"""Alembic migrations build exactly the schema the ORM models describe."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.db import create_db_engine
from ledgerline.models import Base
from ledgerline.reconcile import reconcile
from tests.support import API_KEY, FakeClock, authorize, deposit, open_account, post

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def migrate(url: str, revision: str = "head") -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision)
    return config


def test_upgrade_head_matches_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    migrate(url)
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert differences == []


def test_the_api_runs_on_a_migrated_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    migrate(url)
    app = create_app(Settings(database_url=url, api_keys=[API_KEY], _env_file=None))
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        cash = open_account(client, name="Cash")
        wallet = open_account(client, name="Wallet", account_type="liability")
        deposit(client, cash, wallet, 100)
        assert client.get(f"/v1/accounts/{wallet['id']}/balance").json()["balance"] == 100


def test_downgrade_removes_every_table(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.downgrade(migrate(url), "base")
    engine = create_db_engine(url)
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()


def seed_revision_0001(url: str) -> dict[str, str]:
    """Write a small ledger in the 0001 schema: a deposit, a reversal and an idempotency key."""
    ids = {name: uuid4().hex for name in ("cash", "wallet", "deposit", "reversal")}
    engine = create_db_engine(url)
    try:
        with engine.begin() as connection:
            for name, kind, side in (("cash", "asset", "debit"), ("wallet", "liability", "credit")):
                connection.execute(
                    text(
                        "INSERT INTO accounts VALUES (:id, :name, :kind, 'USD', :side, false, '{}',"
                        " 0, 2, '2026-10-30 10:00:00.000000')"
                    ),
                    {"id": ids[name], "name": name, "kind": kind, "side": side},
                )
            for transaction, reverses, moment in (
                ("deposit", None, "10:00:01"),
                ("reversal", ids["deposit"], "10:00:02"),
            ):
                connection.execute(
                    text("INSERT INTO transactions VALUES (:id, NULL, '{}', :at, :reverses)"),
                    {
                        "id": ids[transaction],
                        "at": f"2026-10-30 {moment}.000000",
                        "reverses": reverses,
                    },
                )
                sign = 1 if transaction == "deposit" else -1
                for account, amount, after in (("cash", 100, 100), ("wallet", -100, 100)):
                    connection.execute(
                        text(
                            "INSERT INTO entries (transaction_id, account_id, amount, currency,"
                            " balance_after) VALUES (:t, :a, :amount, 'USD', :after)"
                        ),
                        {
                            "t": ids[transaction],
                            "a": ids[account],
                            "amount": sign * amount,
                            "after": after if sign == 1 else 0,
                        },
                    )
            connection.execute(
                text(
                    "INSERT INTO idempotency_keys VALUES ('client', 'deposit-1', :fp, '{}',"
                    " '2026-10-30 10:00:01.000000')"
                ),
                {"fp": "0" * 64},
            )
    finally:
        engine.dispose()
    return ids


def test_upgrading_a_ledger_that_already_has_data_keeps_every_row(tmp_path: Path) -> None:
    # SQLite rebuilds `transactions` to add the hold link. The table is
    # referenced by entries (and by itself, through reverses_id), which is
    # what made the rebuild fail while foreign keys were enforced.
    url = f"sqlite:///{tmp_path / 'existing.db'}"
    migrate(url, "0001")
    ids = seed_revision_0001(url)
    migrate(url)

    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            rows = connection.execute(
                text("SELECT id, reverses_id, hold_id FROM transactions ORDER BY posted_at")
            ).all()
            entries = connection.execute(text("SELECT count(*) FROM entries")).scalar()
            keys = connection.execute(text("SELECT count(*) FROM idempotency_keys")).scalar()
        assert [tuple(row) for row in rows] == [
            (ids["deposit"], None, None),
            (ids["reversal"], ids["deposit"], None),
        ]
        assert (entries, keys) == (4, 1)
        with Session(engine) as session:
            assert reconcile(session).ok
    finally:
        engine.dispose()


def test_holds_work_on_an_upgraded_ledger_and_downgrading_keeps_the_journal(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'existing.db'}"
    migrate(url, "0001")
    seed_revision_0001(url)
    config = migrate(url)
    clock = FakeClock()
    settings = Settings(database_url=url, api_keys=[API_KEY], _env_file=None)
    app = create_app(settings, clock=clock)
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        alice = open_account(client, name="Alice", account_type="liability")
        bob = open_account(client, name="Bob", account_type="liability")
        deposit(client, open_account(client, name="Cash"), alice, 1_000)
        hold = authorize(client, alice, bob, 600, expires_at=clock.now + timedelta(hours=1))
        assert post(client, f"/v1/holds/{hold['id']}/capture", {"amount": 250}).status_code == 200

    command.downgrade(config, "0001")
    engine = create_db_engine(url)
    try:
        inspector = inspect(engine)
        assert "holds" not in inspector.get_table_names()
        assert "hold_id" not in {column["name"] for column in inspector.get_columns("transactions")}
        with engine.connect() as connection:
            # The capture stays in the journal as an ordinary transaction.
            assert connection.execute(text("SELECT count(*) FROM transactions")).scalar() == 4
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_offline_sql_generation_still_works_for_sqlite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The rebuild uses an explicit table definition, so it needs no live database.
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'never-created.db'}")
    command.upgrade(config, "head", sql=True)
    script = capsys.readouterr().out
    assert "CREATE TABLE holds" in script
    assert "ALTER TABLE _alembic_tmp_transactions RENAME TO transactions" in script
    assert not (tmp_path / "never-created.db").exists()
