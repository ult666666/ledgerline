"""Property-based test: random sequences of ledger operations keep every invariant.

Hypothesis drives the real HTTP API with random sequences of account opening,
transfers, multi-leg (and multi-currency) journal transactions, reversals,
idempotent replays and key reuse. A simple in-memory model predicts each
outcome, and after every step the database is checked against the model and
against the double-entry invariants. When a sequence fails, Hypothesis shrinks
it to a minimal reproduction.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ledgerline.domain import NormalBalance, balance_change
from ledgerline.models import Account, Entry
from ledgerline.reconcile import reconcile
from tests.conftest import reset_database
from tests.support import Json, problem_type

CURRENCIES = ("USD", "EUR")
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")
amounts = st.integers(min_value=1, max_value=5_000)
signed_amounts = st.integers(min_value=-5_000, max_value=5_000).filter(bool)


@dataclass
class ModelAccount:
    id: str
    currency: str
    normal_balance: NormalBalance
    allow_negative: bool
    balance: int = 0


@dataclass
class Posted:
    """A successful money-moving request, kept so it can be replayed or reversed."""

    path: str
    body: Json | None
    key: str
    response: Json
    changes: dict[str, int]


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self, client: TestClient, engine: Engine) -> None:
        super().__init__()
        self.client = client
        self.engine = engine
        reset_database(engine)
        self.accounts: dict[str, ModelAccount] = {}
        self.posted: list[Posted] = []
        self.reversible: list[Posted] = []
        self.reversed: list[Posted] = []
        self.reversals: list[Posted] = []

    # Rules

    @rule(
        currency=st.sampled_from(CURRENCIES),
        account_type=st.sampled_from(ACCOUNT_TYPES),
        allow_negative=st.booleans(),
    )
    def open_account(self, currency: str, account_type: str, allow_negative: bool) -> None:
        response = self.client.post(
            "/v1/accounts",
            json={
                "name": f"{account_type} {len(self.accounts)}",
                "type": account_type,
                "currency": currency,
                "allow_negative": allow_negative,
            },
        )
        assert response.status_code == 201, response.text
        account = response.json()
        self.accounts[account["id"]] = ModelAccount(
            id=account["id"],
            currency=currency,
            normal_balance=NormalBalance(account["normal_balance"]),
            allow_negative=allow_negative,
        )

    @precondition(lambda self: bool(self._transfer_pairs()))
    @rule(data=st.data(), amount=amounts)
    def transfer(self, data: st.DataObject, amount: int) -> None:
        source, destination = data.draw(st.sampled_from(self._transfer_pairs()))
        body = {
            "from_account_id": source.id,
            "to_account_id": destination.id,
            "amount": amount,
            "currency": source.currency,
        }
        self._post("/v1/transfers", body, {source.id: -amount, destination.id: amount})

    @precondition(lambda self: bool(self._currencies_with_two_accounts()))
    @rule(data=st.data())
    def journal(self, data: st.DataObject) -> None:
        currencies = data.draw(
            st.lists(
                st.sampled_from(self._currencies_with_two_accounts()),
                min_size=1,
                max_size=2,
                unique=True,
            )
        )
        entries: list[Json] = []
        changes: dict[str, int] = {}
        for currency in currencies:
            candidates = [a for a in self.accounts.values() if a.currency == currency]
            legs = data.draw(
                st.lists(
                    st.sampled_from(candidates),
                    min_size=2,
                    max_size=4,
                    unique_by=lambda account: account.id,
                )
            )
            leg_amounts = data.draw(
                st.lists(signed_amounts, min_size=len(legs) - 1, max_size=len(legs) - 1)
            )
            balancing = -sum(leg_amounts)
            if balancing == 0:
                legs = legs[:-1]  # the other legs already balance on their own
            else:
                leg_amounts.append(balancing)
            for account, amount in zip(legs, leg_amounts, strict=True):
                entries.append({"account_id": account.id, "amount": amount, "currency": currency})
                changes[account.id] = balance_change(account.normal_balance, amount)
        self._post("/v1/transactions", {"entries": entries}, changes)

    @precondition(lambda self: bool(self.reversible))
    @rule(data=st.data())
    def reverse(self, data: st.DataObject) -> None:
        original = data.draw(st.sampled_from(self.reversible))
        changes = {account_id: -change for account_id, change in original.changes.items()}
        reversal = self._post(f"/v1/transactions/{original.response['id']}/reverse", None, changes)
        if reversal is not None:
            assert reversal.response["reverses_transaction_id"] == original.response["id"]
            self.reversible.remove(original)
            self.reversed.append(original)
            self.reversals.append(reversal)

    @precondition(lambda self: bool(self.reversed))
    @rule(data=st.data())
    def reverse_twice(self, data: st.DataObject) -> None:
        original = data.draw(st.sampled_from(self.reversed))
        response = self._send(f"/v1/transactions/{original.response['id']}/reverse", None)
        assert response.status_code == 409
        assert problem_type(response) == "already-reversed"

    @precondition(lambda self: bool(self.reversals))
    @rule(data=st.data())
    def reverse_a_reversal(self, data: st.DataObject) -> None:
        reversal = data.draw(st.sampled_from(self.reversals))
        response = self._send(f"/v1/transactions/{reversal.response['id']}/reverse", None)
        assert response.status_code == 422
        assert problem_type(response) == "reversal-not-allowed"

    @precondition(lambda self: bool(self.posted))
    @rule(data=st.data())
    def replay(self, data: st.DataObject) -> None:
        earlier = data.draw(st.sampled_from(self.posted))
        response = self._send(earlier.path, earlier.body, key=earlier.key)
        assert response.status_code == 201, response.text
        assert response.headers["Idempotent-Replayed"] == "true"
        assert response.json() == earlier.response

    @precondition(lambda self: bool(self.posted))
    @rule(data=st.data())
    def reuse_key_for_a_different_request(self, data: st.DataObject) -> None:
        earlier = data.draw(st.sampled_from(self.posted))
        different = {**(earlier.body or {}), "description": f"not the same {uuid4()}"}
        response = self._send(earlier.path, different, key=earlier.key)
        assert response.status_code == 409
        assert problem_type(response) == "idempotency-key-reused"

    # Invariants, checked after every step

    @invariant()
    def database_agrees_with_the_model_and_double_entry_holds(self) -> None:
        with Session(self.engine) as session:
            stored = {
                str(account_id): balance
                for account_id, balance in session.execute(select(Account.id, Account.balance))
            }
            entries = session.execute(
                select(Entry.account_id, Entry.amount, Entry.balance_after).order_by(Entry.id)
            ).all()
            report = reconcile(session)

        # Stored balances match the model (so replays and rejected requests
        # changed nothing), sum(entries) matches stored balances, every
        # transaction balances per currency and the trial balance is zero.
        assert stored == {account_id: a.balance for account_id, a in self.accounts.items()}
        assert report.ok, report.render()

        # No account that must stay non-negative ever went below zero.
        for account_id, account in self.accounts.items():
            if not account.allow_negative:
                assert stored[account_id] >= 0

        # Statement running balances are consistent with the entries.
        running: dict[str, int] = defaultdict(int)
        for entry_account_id, amount, balance_after in entries:
            account = self.accounts[str(entry_account_id)]
            running[account.id] += balance_change(account.normal_balance, amount)
            assert balance_after == running[account.id]

    # Helpers

    def _transfer_pairs(self) -> list[tuple[ModelAccount, ModelAccount]]:
        return [
            (source, destination)
            for source in self.accounts.values()
            for destination in self.accounts.values()
            if source.id != destination.id
            and source.currency == destination.currency
            and source.normal_balance is destination.normal_balance
        ]

    def _currencies_with_two_accounts(self) -> list[str]:
        return [
            currency
            for currency in CURRENCIES
            if sum(a.currency == currency for a in self.accounts.values()) >= 2
        ]

    def _send(self, path: str, body: Json | None, *, key: str | None = None) -> Any:
        return self.client.post(path, json=body, headers={"Idempotency-Key": key or str(uuid4())})

    def _post(self, path: str, body: Json | None, changes: dict[str, int]) -> Posted | None:
        """Send a money-moving request and check it against the model's prediction."""
        allowed = all(
            self.accounts[account_id].allow_negative
            or self.accounts[account_id].balance + change >= 0
            for account_id, change in changes.items()
        )
        key = str(uuid4())
        response = self._send(path, body, key=key)
        if not allowed:
            assert response.status_code == 422, response.text
            assert problem_type(response) == "insufficient-funds"
            return None

        assert response.status_code == 201, response.text
        for account_id, change in changes.items():
            self.accounts[account_id].balance += change
        posted = Posted(path, body, key, response.json(), changes)
        self.posted.append(posted)
        if body is not None:  # reversals are not themselves reversible
            self.reversible.append(posted)
        return posted


def test_random_operation_sequences_preserve_ledger_invariants(
    client: TestClient, db: Engine
) -> None:
    class Machine(LedgerMachine):
        def __init__(self) -> None:
            super().__init__(client, db)

    run_state_machine_as_test(
        Machine, settings=settings(max_examples=60, stateful_step_count=30, deadline=None)
    )
