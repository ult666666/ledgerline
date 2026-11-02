"""Reconciliation catches any drift between cached balances and the journal."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update
from sqlalchemy.orm import Session

from ledgerline.cli import main
from ledgerline.domain import HoldStatus
from ledgerline.models import Account, Entry, Hold, Transaction
from ledgerline.reconcile import hold_problems, reconcile
from tests.support import FakeClock, Json, authorize, deposit, open_account, post


def open_account_row(engine: Engine) -> UUID:
    """A second credit-normal wallet, written directly."""
    account_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(Account).values(
                id=account_id,
                name="Other wallet",
                type="liability",
                currency="USD",
                normal_balance="credit",
                allow_negative=False,
                metadata={},
                balance=0,
                version=0,
                created_at=datetime.now(UTC),
            )
        )
    return account_id


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
        "accounts: 2  transactions: 2  entries: 4  holds: 0",
        "[ok] stored balances equal the sum of their entries",
        "[ok] every transaction sums to zero per currency",
        "[ok] every entry uses its account's currency",
        "[ok] trial balance is zero per currency (USD 0)",
        "[ok] every hold's state, amounts and timestamps follow the hold state machine",
        "[ok] active holds fit within the balance of every account that cannot go negative",
        "[ok] every captured hold is in the journal exactly once, for the captured amount",
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


class TestHolds:
    """Each hold invariant, broken directly in the database past the service's rules."""

    @pytest.fixture
    def wallets(self, clocked_client: TestClient) -> tuple[Json, Json]:
        cash = open_account(clocked_client, name="Cash")
        alice = open_account(clocked_client, name="Alice", account_type="liability")
        bob = open_account(clocked_client, name="Bob", account_type="liability")
        deposit(clocked_client, cash, alice, 1_000)
        return alice, bob

    @pytest.fixture
    def captured(
        self, clocked_client: TestClient, clock: FakeClock, wallets: tuple[Json, Json]
    ) -> Json:
        """A hold of 600 from Alice to Bob, captured for 400."""
        alice, bob = wallets
        hold = authorize(clocked_client, alice, bob, 600, expires_at=clock.now + timedelta(hours=1))
        clock.advance(minutes=1)
        response = post(clocked_client, f"/v1/holds/{hold['id']}/capture", {"amount": 400})
        assert response.status_code == 200, response.text
        captured: Json = response.json()
        return captured

    @staticmethod
    def report(engine: Engine, clock: FakeClock) -> str:
        with Session(engine) as session:
            return reconcile(session, now=clock.now).render()

    def test_a_ledger_with_every_kind_of_hold_is_consistent(
        self,
        db: Engine,
        clocked_client: TestClient,
        clock: FakeClock,
        wallets: tuple[Json, Json],
        captured: Json,
    ) -> None:
        alice, bob = wallets
        authorize(clocked_client, alice, bob, 100, expires_at=clock.now + timedelta(hours=1))
        voided = authorize(
            clocked_client, alice, bob, 100, expires_at=clock.now + timedelta(hours=1)
        )
        post(clocked_client, f"/v1/holds/{voided['id']}/void", None)
        authorize(clocked_client, alice, bob, 100, expires_at=clock.now + timedelta(seconds=5))
        clock.advance(seconds=10)  # the last hold expires
        with Session(db) as session:
            report = reconcile(session, now=clock.now)
        assert report.ok, report.render()
        assert report.holds == 4
        assert report.render().splitlines()[0].endswith("holds: 4")

    def test_detects_holds_that_exceed_the_balance(
        self, db: Engine, clock: FakeClock, wallets: tuple[Json, Json]
    ) -> None:
        alice, bob = wallets
        expires_at = clock.now + timedelta(hours=1)
        with db.begin() as connection:
            connection.execute(
                insert(Hold).values(
                    id=uuid4(),
                    from_account_id=UUID(alice["id"]),
                    to_account_id=UUID(bob["id"]),
                    amount=1_001,
                    currency="USD",
                    status=HoldStatus.ACTIVE,
                    metadata={},
                    created_at=clock.now,
                    expires_at=expires_at,
                )
            )
        rendered = self.report(db, clock)
        assert "[FAIL] active holds fit within the balance" in rendered
        assert f"account {alice['id']}: balance 1000, active holds 1001" in rendered
        # Once the hold expires it no longer reserves anything.
        clock.now = expires_at
        assert self.report(db, clock).endswith("Ledger is consistent.")

    @pytest.mark.parametrize(
        ("closed_at", "problem"),
        [
            (timedelta(hours=2), "after it had expired"),
            (timedelta(minutes=-5), "before it was created"),
        ],
    )
    def test_detects_a_hold_closed_outside_its_active_window(
        self, db: Engine, clock: FakeClock, captured: Json, closed_at: timedelta, problem: str
    ) -> None:
        moved = datetime.fromisoformat(captured["created_at"]) + closed_at
        with db.begin() as connection:
            connection.execute(
                update(Hold).where(Hold.id == UUID(captured["id"])).values(closed_at=moved)
            )
            # Keep the journal consistent with the hold, so only the state check fails.
            connection.execute(
                update(Transaction)
                .where(Transaction.id == UUID(captured["capture_transaction_id"]))
                .values(posted_at=moved)
            )
        rendered = self.report(db, clock)
        assert "[FAIL] every hold's state, amounts and timestamps" in rendered
        assert f"hold {captured['id']}: captured at" in rendered
        assert problem in rendered

    def test_detects_a_capture_missing_from_the_journal(
        self, db: Engine, clock: FakeClock, captured: Json
    ) -> None:
        with db.begin() as connection:
            connection.execute(
                update(Transaction)
                .where(Transaction.id == UUID(captured["capture_transaction_id"]))
                .values(hold_id=None)
            )
        rendered = self.report(db, clock)
        assert "[FAIL] every captured hold is in the journal exactly once" in rendered
        assert "captured, but no journal transaction records the capture" in rendered

    def test_detects_a_capture_posted_for_the_wrong_amount(
        self, db: Engine, clock: FakeClock, captured: Json, wallets: tuple[Json, Json]
    ) -> None:
        with db.begin() as connection:
            connection.execute(
                update(Hold).where(Hold.id == UUID(captured["id"])).values(captured_amount=500)
            )
        rendered = self.report(db, clock)
        assert "[FAIL] every captured hold is in the journal exactly once" in rendered
        assert "not 500 from" in rendered

    def test_detects_a_capture_posted_at_another_time(
        self, db: Engine, clock: FakeClock, captured: Json
    ) -> None:
        with db.begin() as connection:
            connection.execute(
                update(Transaction)
                .where(Transaction.id == UUID(captured["capture_transaction_id"]))
                .values(posted_at=clock.now + timedelta(seconds=1))
            )
        assert "was not posted when the hold closed" in self.report(db, clock)

    def test_detects_a_transaction_capturing_a_hold_that_was_not_captured(
        self,
        db: Engine,
        clocked_client: TestClient,
        clock: FakeClock,
        wallets: tuple[Json, Json],
    ) -> None:
        alice, bob = wallets
        active = authorize(
            clocked_client, alice, bob, 100, expires_at=clock.now + timedelta(hours=1)
        )
        stray = insert_transaction(db, (alice["id"], 100, "USD"), (bob["id"], -100, "USD"))
        with db.begin() as connection:
            connection.execute(
                update(Transaction)
                .where(Transaction.id == stray)
                .values(hold_id=UUID(active["id"]))
            )
        assert "the hold is active, yet a journal transaction captures it" in self.report(db, clock)


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        (("expired", 100, None, None), "stored status 'expired'"),
        (("active", 0, None, None), "authorized amount 0 is not positive"),
        (("active", 100, None, "closed"), "an active hold has a closing time"),
        (("active", 100, 50, None), "an active hold has a closing time or a captured amount"),
        (("captured", 100, 101, "closed"), "captured 101 of an authorized 100"),
        (("captured", 100, None, "closed"), "a captured hold has no captured amount"),
        (("captured", 100, 50, None), "a captured hold has no closed_at"),
        (("voided", 100, 50, "closed"), "a voided hold has a captured amount"),
    ],
)
def test_hold_problems_name_every_way_a_stored_hold_breaks_the_state_machine(
    fields: tuple[str, int, int | None, str | None], problem: str
) -> None:
    status, amount, captured_amount, closed = fields
    created_at = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    expires_at = created_at + timedelta(hours=1)
    closed_at = created_at + timedelta(minutes=5) if closed else None
    problems = hold_problems(status, amount, captured_amount, created_at, expires_at, closed_at)
    assert any(problem in found for found in problems), problems


def test_hold_problems_accept_every_valid_path() -> None:
    created_at = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    expires_at = created_at + timedelta(hours=1)
    closed_at = expires_at - timedelta(microseconds=1)
    for status, captured in [
        ("active", None),
        ("captured", 1),
        ("captured", 100),
        ("voided", None),
    ]:
        closed = None if status == "active" else closed_at
        assert hold_problems(status, 100, captured, created_at, expires_at, closed) == []
    assert hold_problems("active", 100, None, created_at, created_at, None) == [
        "expires_at is not later than created_at"
    ]


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

    def test_exits_one_when_holds_exceed_a_balance(
        self, db: Engine, ledger: tuple[Json, Json], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, wallet = ledger
        other = open_account_row(db)
        now = datetime.now(UTC)
        with db.begin() as connection:
            connection.execute(
                insert(Hold).values(
                    id=uuid4(),
                    from_account_id=UUID(wallet["id"]),
                    to_account_id=other,
                    amount=1_251,
                    currency="USD",
                    status=HoldStatus.ACTIVE,
                    metadata={},
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                )
            )
        assert main(["reconcile"]) == 1
        assert "[FAIL] active holds fit within the balance" in capsys.readouterr().out

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
