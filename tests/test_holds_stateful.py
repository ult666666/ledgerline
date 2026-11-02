"""Property-based test of the hold state machine against a simple in-memory model.

Hypothesis drives the HTTP API with random sequences of hold authorizations,
full and partial captures, voids, transfers, refunds of captures, clock moves
(which expire holds), idempotent replays and key reuse. The model below knows
nothing about the service's code: it predicts every response (status code,
problem type, the hold's new state) and every account's ledger, held and
available balance from the rules in docs/DESIGN.md alone.

After every step the test checks the database against the model, checks that
each hold's observed status only ever moved along an edge of the hold state
machine, and runs ``reconcile``. A failing sequence is shrunk to a minimal one.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from hypothesis import event
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.domain import HOLD_TRANSITIONS, HoldStatus
from ledgerline.models import Hold
from ledgerline.reconcile import reconcile
from tests.conftest import reset_database
from tests.support import API_KEY, FakeClock, Json, problem_type

amounts = st.integers(min_value=1, max_value=3_000)


@dataclass
class ModelAccount:
    id: str
    side: str
    """Accounts can only move money to accounts on the same normal-balance side."""
    allow_negative: bool
    balance: int = 0


@dataclass
class ModelHold:
    id: str
    source: ModelAccount
    destination: ModelAccount
    amount: int
    expires_at: datetime
    stored: HoldStatus = HoldStatus.ACTIVE
    captured: int | None = None
    capture_transaction_id: str | None = None
    refunded: bool = False

    def status(self, now: datetime) -> HoldStatus:
        if self.stored is HoldStatus.ACTIVE and now >= self.expires_at:
            return HoldStatus.EXPIRED
        return self.stored


@dataclass
class Write:
    """A successful hold write, kept so it can be replayed or its key reused."""

    path: str
    body: Json | None
    key: str
    status: int
    response: Json


class HoldMachine(RuleBasedStateMachine):
    def __init__(self, client: TestClient, engine: Engine, clock: FakeClock) -> None:
        super().__init__()
        self.client = client
        self.engine = engine
        self.clock = clock
        reset_database(engine)
        self.accounts: list[ModelAccount] = []
        self.holds: list[ModelHold] = []
        self.writes: list[Write] = []
        self.observed: dict[str, HoldStatus] = {}

    @initialize()
    def open_accounts(self) -> None:
        # Two books: credit-normal wallets and debit-normal bank accounts, each
        # with one account that may go negative (the source of new money).
        for kind, allow_negative in [
            ("liability", True),
            ("liability", False),
            ("liability", False),
            ("liability", False),
            ("asset", True),
            ("asset", False),
        ]:
            response = self.client.post(
                "/v1/accounts",
                json={
                    "name": f"{kind} {len(self.accounts)}",
                    "type": kind,
                    "currency": "USD",
                    "allow_negative": allow_negative,
                },
            )
            assert response.status_code == 201, response.text
            self.accounts.append(ModelAccount(response.json()["id"], kind, allow_negative))
        for account in self.accounts:
            if not account.allow_negative:
                [source] = [a for a in self._partners(account) if a.allow_negative]
                self._transfer(source, account, 3_000)

    # Rules

    @rule(data=st.data(), amount=st.integers(min_value=1, max_value=1_000))
    def fund(self, data: st.DataObject, amount: int) -> None:
        source = data.draw(st.sampled_from([a for a in self.accounts if a.allow_negative]))
        destination = data.draw(st.sampled_from(self._partners(source)))
        self._transfer(source, destination, amount)

    @rule(data=st.data(), amount=amounts)
    def transfer(self, data: st.DataObject, amount: int) -> None:
        source, destination = data.draw(st.sampled_from(self._pairs()))
        self._transfer(source, destination, amount)

    @rule(data=st.data(), amount=amounts, ttl=st.integers(min_value=1, max_value=300))
    def authorize(self, data: st.DataObject, amount: int, ttl: int) -> None:
        source, destination = data.draw(st.sampled_from(self._pairs()))
        expires_at = self.clock.now + timedelta(seconds=ttl)
        body = {
            "from_account_id": source.id,
            "to_account_id": destination.id,
            "amount": amount,
            "currency": "USD",
            "expires_at": expires_at.isoformat(),
        }
        response = self._write("/v1/holds", body)
        if not self._can_spend(source, amount):
            event(f"authorize refused{self._because_of_holds(source, amount)}")
            self._expect_problem(response, 422, "insufficient-funds")
            return
        event("authorize ok")
        assert response.status_code == 201, response.text
        hold = response.json()
        assert (hold["status"], hold["amount"]) == ("active", amount)
        self.holds.append(ModelHold(hold["id"], source, destination, amount, expires_at))

    @precondition(lambda self: bool(self.holds))
    @rule(data=st.data())
    def capture(self, data: st.DataObject) -> None:
        hold = self._pick_hold(data)
        requested = data.draw(st.none() | st.integers(min_value=1, max_value=hold.amount + 50))
        body = None if requested is None else {"amount": requested}
        response = self._write(f"/v1/holds/{hold.id}/capture", body)
        if self._refused_as_inactive(hold, response):
            event(f"capture refused: {hold.status(self.clock.now)}")
            return
        amount = hold.amount if requested is None else requested
        if amount > hold.amount:
            event("capture refused: more than authorized")
            self._expect_problem(response, 422, "invalid-hold")
            return
        event("capture ok: " + ("full" if amount == hold.amount else "partial"))
        assert response.status_code == 200, response.text
        captured = response.json()
        assert (captured["status"], captured["captured_amount"]) == ("captured", amount)
        hold.source.balance -= amount
        hold.destination.balance += amount
        hold.stored, hold.captured = HoldStatus.CAPTURED, amount
        hold.capture_transaction_id = captured["capture_transaction_id"]

    @precondition(lambda self: bool(self.holds))
    @rule(data=st.data())
    def void(self, data: st.DataObject) -> None:
        hold = self._pick_hold(data)
        response = self._write(f"/v1/holds/{hold.id}/void", None)
        if self._refused_as_inactive(hold, response):
            event(f"void refused: {hold.status(self.clock.now)}")
            return
        event("void ok")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "voided"
        hold.stored = HoldStatus.VOIDED

    @precondition(lambda self: any(h.capture_transaction_id for h in self.holds))
    @rule(data=st.data())
    def refund(self, data: st.DataObject) -> None:
        hold = data.draw(st.sampled_from([h for h in self.holds if h.capture_transaction_id]))
        assert hold.captured is not None
        response = self._send(f"/v1/transactions/{hold.capture_transaction_id}/reverse", None)
        if hold.refunded:
            event("refund refused: already refunded")
            self._expect_problem(response, 409, "already-reversed")
        elif not self._can_spend(hold.destination, hold.captured):
            event(f"refund refused{self._because_of_holds(hold.destination, hold.captured)}")
            self._expect_problem(response, 422, "insufficient-funds")
        else:
            event("refund ok")
            assert response.status_code == 201, response.text
            hold.destination.balance -= hold.captured
            hold.source.balance += hold.captured
            hold.refunded = True  # the hold itself stays captured

    @rule(seconds=st.integers(min_value=1, max_value=200))
    def advance_clock(self, seconds: int) -> None:
        self.clock.advance(seconds=seconds)

    @precondition(lambda self: bool(self.writes))
    @rule(data=st.data())
    def replay(self, data: st.DataObject) -> None:
        earlier = data.draw(st.sampled_from(self.writes))
        response = self._send(earlier.path, earlier.body, key=earlier.key)
        assert response.status_code == earlier.status, response.text
        assert response.headers["Idempotent-Replayed"] == "true"
        assert response.json() == earlier.response

    @precondition(lambda self: bool(self.writes))
    @rule(data=st.data())
    def reuse_key_for_a_different_request(self, data: st.DataObject) -> None:
        earlier = data.draw(st.sampled_from(self.writes))
        if earlier.body is None:  # a void or a full capture: send the key to the other one
            other = "/capture" if earlier.path.endswith("/void") else "/void"
            path, body = earlier.path.rsplit("/", 1)[0] + other, None
        else:
            path, body = earlier.path, {**earlier.body, "description": f"different {uuid4()}"}
        response = self._send(path, body, key=earlier.key)
        self._expect_problem(response, 409, "idempotency-key-reused")

    # Invariants, checked after every step

    @invariant()
    def accounts_agree_with_the_model(self) -> None:
        listed = {a["id"]: a for a in self.client.get("/v1/accounts").json()["data"]}
        for account in self.accounts:
            held = self._held(account)
            reported = listed[account.id]
            assert (
                reported["balance"],
                reported["held_amount"],
                reported["available_balance"],
            ) == (account.balance, held, account.balance - held)
            if not account.allow_negative:
                assert held <= account.balance

    @invariant()
    def holds_agree_with_the_model_and_only_move_along_the_state_machine(self) -> None:
        with Session(self.engine) as session:
            stored = {
                str(hold_id): (status, captured)
                for hold_id, status, captured in session.execute(
                    select(Hold.id, Hold.status, Hold.captured_amount)
                )
            }
            report = reconcile(session, now=self.clock.now)
        assert stored == {h.id: (h.stored, h.captured) for h in self.holds}
        for hold in self.holds:
            status = hold.status(self.clock.now)
            previous = self.observed.get(hold.id, HoldStatus.ACTIVE)
            assert status == previous or status in HOLD_TRANSITIONS[previous], (previous, status)
            self.observed[hold.id] = status
        assert report.ok, report.render()

    # Helpers

    def _pick_hold(self, data: st.DataObject) -> ModelHold:
        """Usually a hold that is still active, sometimes any hold (to test refusals)."""
        active = [h for h in self.holds if h.status(self.clock.now) is HoldStatus.ACTIVE]
        prefer_active: bool = data.draw(st.integers(min_value=0, max_value=3)) > 0
        return data.draw(st.sampled_from(active if active and prefer_active else self.holds))

    def _pairs(self) -> list[tuple[ModelAccount, ModelAccount]]:
        return [(a, b) for a in self.accounts for b in self._partners(a)]

    def _partners(self, account: ModelAccount) -> list[ModelAccount]:
        return [b for b in self.accounts if b.side == account.side and b.id != account.id]

    def _held(self, account: ModelAccount) -> int:
        return sum(
            h.amount
            for h in self.holds
            if h.source is account and h.status(self.clock.now) is HoldStatus.ACTIVE
        )

    def _can_spend(self, account: ModelAccount, amount: int) -> bool:
        return account.allow_negative or account.balance - self._held(account) >= amount

    @staticmethod
    def _because_of_holds(account: ModelAccount, amount: int) -> str:
        # Labels the refusals that only holds explain: the ledger balance would cover it.
        return ": only because of holds" if account.balance >= amount else ""

    def _transfer(self, source: ModelAccount, destination: ModelAccount, amount: int) -> None:
        body = {
            "from_account_id": source.id,
            "to_account_id": destination.id,
            "amount": amount,
            "currency": "USD",
        }
        response = self._send("/v1/transfers", body)
        if not self._can_spend(source, amount):
            event(f"transfer refused{self._because_of_holds(source, amount)}")
            self._expect_problem(response, 422, "insufficient-funds")
            return
        assert response.status_code == 201, response.text
        if not source.allow_negative:
            event("transfer ok")
        source.balance -= amount
        destination.balance += amount

    def _refused_as_inactive(self, hold: ModelHold, response: Any) -> bool:
        status = hold.status(self.clock.now)
        if status is HoldStatus.EXPIRED:
            self._expect_problem(response, 409, "hold-expired")
        elif status is not HoldStatus.ACTIVE:
            self._expect_problem(response, 409, "hold-not-active")
            assert response.json()["hold_status"] == status
        return status is not HoldStatus.ACTIVE

    def _send(self, path: str, body: Json | None, *, key: str | None = None) -> Any:
        return self.client.post(path, json=body, headers={"Idempotency-Key": key or str(uuid4())})

    def _write(self, path: str, body: Json | None) -> Any:
        """Send a hold write with a fresh key, remembering it if it succeeds."""
        key = str(uuid4())
        response = self._send(path, body, key=key)
        if response.status_code in (200, 201):
            self.writes.append(Write(path, body, key, response.status_code, response.json()))
        return response

    @staticmethod
    def _expect_problem(response: Any, status: int, kind: str) -> None:
        assert response.status_code == status, response.text
        assert problem_type(response) == kind


def test_random_hold_sequences_match_the_model(settings: Settings, db: Engine) -> None:
    clock = FakeClock()
    app = create_app(settings, engine=db, clock=clock)
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as client:

        class Machine(HoldMachine):
            def __init__(self) -> None:
                super().__init__(client, db, clock)

        run_state_machine_as_test(
            Machine,
            settings=hypothesis_settings(max_examples=40, stateful_step_count=35, deadline=None),
        )
