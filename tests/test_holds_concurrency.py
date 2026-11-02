"""Holds under concurrency: authorizations never oversubscribe, and one capture or void wins.

As in test_concurrency.py, the threaded tests queue for the write lock on
SQLite and really race on PostgreSQL. Clients here retry a ``concurrent-update``
with the same request and key, as the API tells them to, so every request
ends with a definite answer and the tests can assert exact counts. The last
tests force the conflict paths deterministically.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from ledgerline.db import create_session_factory
from ledgerline.domain import HoldStatus
from ledgerline.errors import ConcurrentUpdate, HoldNotActive, InsufficientFunds
from ledgerline.idempotency import IdempotencyRequest, request_fingerprint
from ledgerline.models import Hold, Transaction
from ledgerline.repository import LedgerRepository
from ledgerline.schemas import HoldCapture, HoldCreate, HoldOut
from ledgerline.service import LedgerService, WriteResult
from tests.test_concurrency import (
    assert_reconciles,
    funded_wallets,
    open_wallet,
    run_simultaneously,
    transfer_request,
)

# These tests run on the real clock, so their holds expire far in the future.
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


def keyed(path: str, body: BaseModel | None, key: str | None = None) -> IdempotencyRequest:
    return IdempotencyRequest(
        principal="test-client",
        key=key or str(uuid4()),
        fingerprint=request_fingerprint("POST", path, body),
    )


def hold_request(source: UUID, destination: UUID, amount: int) -> HoldCreate:
    return HoldCreate(
        from_account_id=source,
        to_account_id=destination,
        amount=amount,
        currency="USD",
        expires_at=FAR_FUTURE,
    )


def until_decided[T](call: Callable[[], T]) -> T:
    """Retry concurrent-update conflicts with the same request and key, as a client should."""
    for _ in range(100):
        try:
            return call()
        except ConcurrentUpdate:
            continue
    raise AssertionError("still conflicting after 100 attempts")


def outcome(call: Callable[[], object]) -> str:
    try:
        until_decided(call)
    except InsufficientFunds:
        return "insufficient funds"
    except HoldNotActive:
        return "conflict"
    return "ok"


def authorize(service: LedgerService, source: UUID, destination: UUID, amount: int) -> HoldOut:
    body = hold_request(source, destination, amount)
    result = service.create_hold(body, keyed("/v1/holds", body))
    return result.body


def capture_count(engine: Engine) -> int:
    with Session(engine) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(Transaction.hold_id.is_not(None))
            )
            or 0
        )


def test_concurrent_authorizations_succeed_for_exactly_the_affordable_set(
    service: LedgerService, db: Engine
) -> None:
    source, destination = funded_wallets(service, 1_000)

    def hold_30(_: int) -> str:
        body = hold_request(source, destination, 30)
        request = keyed("/v1/holds", body)
        return outcome(lambda: service.create_hold(body, request))

    outcomes = run_simultaneously(40, hold_30)

    # 1000 // 30 = 33 holds fit; the other 7 find too little available.
    assert outcomes.count("ok") == 33
    assert outcomes.count("insufficient funds") == 7
    account = service.get_account(source)
    assert (account.balance, account.held_amount, account.available_balance) == (1_000, 990, 10)
    assert_reconciles(db)


def test_concurrent_holds_and_transfers_share_one_available_balance(
    service: LedgerService, db: Engine
) -> None:
    source, _ = funded_wallets(service, 1_000)
    destinations = [open_wallet(service, f"Destination {n}") for n in range(4)]

    def spend_30(index: int) -> str:
        destination = destinations[index % 4]
        if index % 2:
            hold = hold_request(source, destination, 30)
            hold_key = keyed("/v1/holds", hold)
            return "hold " + outcome(lambda: service.create_hold(hold, hold_key))
        transfer = transfer_request(source, destination, 30)
        transfer_key = keyed("/v1/transfers", transfer)
        return "transfer " + outcome(lambda: service.transfer(transfer, transfer_key))

    outcomes = run_simultaneously(40, spend_30)

    holds, transfers = outcomes.count("hold ok"), outcomes.count("transfer ok")
    # Holds and postings draw on the same available balance: 33 of either kind fit.
    assert holds + transfers == 33
    account = service.get_account(source)
    assert account.balance == 1_000 - 30 * transfers
    assert account.held_amount == 30 * holds
    assert account.available_balance == 10
    assert sum(service.get_account(d).balance for d in destinations) == 30 * transfers
    assert_reconciles(db)


def test_racing_captures_and_voids_have_exactly_one_winner(
    service: LedgerService, db: Engine
) -> None:
    source, destination = funded_wallets(service, 1_000)
    captured_total = 0
    for _ in range(4):
        hold = authorize(service, source, destination, 100)

        def capture_or_void(index: int, hold_id: UUID = hold.id) -> tuple[str, int]:
            if index % 2:
                body = HoldCapture(amount=10 * (index + 1))
                request = keyed(f"/v1/holds/{hold_id}/capture", body)
                return outcome(lambda: service.capture_hold(hold_id, body, request)), index
            void_key = keyed(f"/v1/holds/{hold_id}/void", None)
            return outcome(lambda: service.void_hold(hold_id, void_key)), index

        results = run_simultaneously(10, capture_or_void)

        winners = [index for result, index in results if result == "ok"]
        assert len(winners) == 1, results
        assert [result for result, _ in results].count("conflict") == 9
        final = service.get_hold(hold.id)
        if winners[0] % 2:
            assert final.status is HoldStatus.CAPTURED
            assert final.captured_amount == 10 * (winners[0] + 1)
            captured_total += final.captured_amount
        else:
            assert final.status is HoldStatus.VOIDED

    account = service.get_account(source)
    assert (account.balance, account.held_amount) == (1_000 - captured_total, 0)
    with Session(db) as session:
        captured_holds = session.scalar(
            select(func.count()).select_from(Hold).where(Hold.status == HoldStatus.CAPTURED)
        )
    assert capture_count(db) == captured_holds  # each capture is in the journal once
    assert_reconciles(db)


def test_duplicate_captures_with_one_key_take_effect_once(
    service: LedgerService, db: Engine
) -> None:
    source, destination = funded_wallets(service, 1_000)
    hold = authorize(service, source, destination, 400)
    body = HoldCapture(amount=250)
    request = keyed(f"/v1/holds/{hold.id}/capture", body, key="capture-once")

    results: list[WriteResult[HoldOut]] = run_simultaneously(
        10, lambda _: until_decided(lambda: service.capture_hold(hold.id, body, request))
    )

    assert [result.replayed for result in results].count(False) == 1
    assert len({result.body.capture_transaction_id for result in results}) == 1
    assert capture_count(db) == 1
    assert service.get_account(source).balance == 750
    assert_reconciles(db)


@pytest.mark.parametrize("winner", ["void", "capture"])
def test_a_capture_that_read_the_hold_before_a_rival_committed_is_refused(
    service: LedgerService, db: Engine, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    source, destination = funded_wallets(service, 1_000)
    hold = authorize(service, source, destination, 400)
    if winner == "void":
        service.void_hold(hold.id, keyed(f"/v1/holds/{hold.id}/void", None))
    else:
        first = HoldCapture(amount=100)
        service.capture_hold(hold.id, first, keyed(f"/v1/holds/{hold.id}/capture", first))
    balances = (service.get_account(source).balance, service.get_account(destination).balance)

    # On PostgreSQL (Read Committed) each statement sees what had committed when
    # it started, so a capture can read the hold before the rival committed and
    # the accounts after it. Its balance compare-and-swap then passes, and only
    # the guarded state transition stands between it and a second posting.
    real_get_hold = LedgerRepository.get_hold
    reads = 0

    def stale_first_read(repo: LedgerRepository, hold_id: UUID) -> Hold | None:
        nonlocal reads
        reads += 1
        found = real_get_hold(repo, hold_id)
        if reads > 1 or found is None:
            return found
        columns = {column.key: getattr(found, column.key) for column in Hold.__mapper__.columns}
        stale = {"status": HoldStatus.ACTIVE, "closed_at": None, "captured_amount": None}
        return Hold(**{**columns, **stale})

    monkeypatch.setattr(LedgerRepository, "get_hold", stale_first_read)
    body = HoldCapture()
    with pytest.raises(HoldNotActive) as caught:
        service.capture_hold(hold.id, body, keyed(f"/v1/holds/{hold.id}/capture", body))

    assert caught.value.extensions["hold_status"] == ("voided" if winner == "void" else "captured")
    assert reads == 2  # the stale attempt rolled back and was retried
    assert capture_count(db) == (0 if winner == "void" else 1)
    assert (service.get_account(source).balance, service.get_account(destination).balance) == (
        balances
    )
    assert_reconciles(db)


def test_a_stale_balance_during_authorization_is_retried(
    service: LedgerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = funded_wallets(service, 1_000)
    real_compare_and_set = LedgerRepository.compare_and_set_balance
    forced_failures = 2

    def lose_the_race_twice(
        repo: LedgerRepository, account_id: UUID, *, expected_version: int, new_balance: int
    ) -> bool:
        nonlocal forced_failures
        if forced_failures:
            forced_failures -= 1
            return False
        return real_compare_and_set(
            repo, account_id, expected_version=expected_version, new_balance=new_balance
        )

    monkeypatch.setattr(LedgerRepository, "compare_and_set_balance", lose_the_race_twice)
    hold = authorize(service, source, destination, 250)

    assert forced_failures == 0
    assert service.get_hold(hold.id).status is HoldStatus.ACTIVE
    account = service.get_account(source)
    assert (account.balance, account.held_amount) == (1_000, 250)


def test_hold_retries_are_bounded_and_leave_nothing_behind(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LedgerService(create_session_factory(db), max_attempts=3)
    source, destination = funded_wallets(service, 1_000)
    attempts = 0

    def always_stale(
        _repo: LedgerRepository, _account_id: UUID, *, expected_version: int, new_balance: int
    ) -> bool:
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(LedgerRepository, "compare_and_set_balance", always_stale)
    body = hold_request(source, destination, 100)
    with pytest.raises(ConcurrentUpdate):
        service.create_hold(body, keyed("/v1/holds", body))

    assert attempts == 3
    with Session(db) as session:
        assert session.scalar(select(func.count()).select_from(Hold)) == 0
    assert service.get_account(source).held_amount == 0
