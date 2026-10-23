"""Reconciliation catches any drift between cached balances and the journal."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update
from sqlalchemy.orm import Session

from ledgerline.cli import main
from ledgerline.models import Account, Entry, Transaction
from ledgerline.reconcile import reconcile
from tests.support import Json, deposit, open_account


@pytest.fixture
def ledger(client: TestClient) -> tuple[Json, Json]:
    cash = open_account(client, name="Cash")
    wallet = open_account(client, name="Wallet", account_type="liability")
    deposit(client, cash, wallet, 1_000)
    deposit(client, cash, wallet, 250)
    return cash, wallet


def run_reconcile(engine: Engine) -> str:
    with Session(engine) as session:
        report = reconcile(session)
    return report.render()


def insert_transaction(engine: Engine, *legs: tuple[str, int, str]) -> UUID:
    """Write journal rows directly, bypassing every rule in the service layer."""
    transaction_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Transaction).values(
                id=transaction_id, metadata={}, posted_at=datetime.now(UTC), reverses_id=None
            )
        )
        for account_id, amount, currency in legs:
            connection.execute(
                insert(Entry).values(
                    transaction_id=transaction_id,
                    account_id=UUID(account_id),
                    amount=amount,
                    currency=currency,
                    balance_after=0,
                )
            )
    return transaction_id


def test_a_ledger_built_through_the_api_is_consistent(
    db: Engine, ledger: tuple[Json, Json]
) -> None:
    with Session(db) as session:
        report = reconcile(session)
    assert report.ok
    assert (report.accounts, report.transactions, report.entries) == (2, 2, 4)
    assert report.trial_balance == {"USD": 0}
    assert report.render().splitlines() == [
        "accounts: 2  transactions: 2  entries: 4",
        "[ok] stored balances equal the sum of their entries",
        "[ok] every transaction sums to zero per currency",
        "[ok] every entry uses its account's currency",
        "[ok] trial balance is zero per currency (USD 0)",
        "Ledger is consistent.",
    ]


def test_an_empty_ledger_is_consistent(db: Engine) -> None:
    assert "trial balance is zero per currency (no entries yet)" in run_reconcile(db)


def test_detects_a_tampered_balance(db: Engine, ledger: tuple[Json, Json]) -> None:
    _, wallet = ledger
    with db.begin() as connection:
        connection.execute(
            update(Account).where(Account.id == UUID(wallet["id"])).values(balance=5_000)
        )
    with Session(db) as session:
        report = reconcile(session)
    assert not report.ok
    [mismatch] = report.balance_mismatches
    assert (mismatch.stored, mismatch.from_entries) == (5_000, 1_250)
    assert f"account {wallet['id']}: stored 5000, entries give 1250" in report.render()
    assert report.render().endswith("Ledger has discrepancies.")


def test_detects_an_unbalanced_transaction(db: Engine, ledger: tuple[Json, Json]) -> None:
    cash, wallet = ledger
    bad = insert_transaction(db, (cash["id"], 100, "USD"), (wallet["id"], -90, "USD"))
    with Session(db) as session:
        report = reconcile(session)
    assert [(u.transaction_id, u.currency, u.total) for u in report.unbalanced_transactions] == [
        (bad, "USD", 10)
    ]
    assert report.trial_balance == {"USD": 10}
    assert "[FAIL] trial balance is zero per currency (USD 10)" in report.render()


def test_detects_an_entry_in_the_wrong_currency(db: Engine, ledger: tuple[Json, Json]) -> None:
    cash, wallet = ledger
    insert_transaction(db, (cash["id"], 5, "EUR"), (wallet["id"], -5, "EUR"))
    with Session(db) as session:
        report = reconcile(session)
    assert len(report.currency_mismatches) == 2
    assert {m.entry_currency for m in report.currency_mismatches} == {"EUR"}
    assert "[FAIL] every entry uses its account's currency" in report.render()


class TestReconcileCommand:
    @pytest.fixture(autouse=True)
    def point_at_test_database(self, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", database_url)

    def test_exits_zero_for_a_consistent_ledger(
        self, ledger: tuple[Json, Json], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["reconcile"]) == 0
        assert "Ledger is consistent." in capsys.readouterr().out

    def test_exits_one_when_something_is_off(
        self, db: Engine, ledger: tuple[Json, Json], capsys: pytest.CaptureFixture[str]
    ) -> None:
        cash, _ = ledger
        with db.begin() as connection:
            connection.execute(
                update(Account).where(Account.id == UUID(cash["id"])).values(balance=1)
            )
        assert main(["reconcile"]) == 1
        assert "Ledger has discrepancies." in capsys.readouterr().out

    def test_exits_two_when_the_schema_is_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path_factory: pytest.TempPathFactory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        empty = tmp_path_factory.mktemp("empty") / "ledger.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{empty}")
        assert main(["reconcile"]) == 2
        assert "alembic upgrade head" in capsys.readouterr().err
