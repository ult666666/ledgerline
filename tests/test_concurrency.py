"""Concurrency control: optimistic balance updates, bounded retries, exactly-once writes.

The two threaded tests are the real thing: on SQLite writers queue for the
database lock, on PostgreSQL they race and the version check plus retries
keep them correct. The other tests force the conflict paths deterministically.
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from ledgerline.db import create_session_factory
from ledgerline.domain import AccountType
from ledgerline.errors import ConcurrentUpdate, InsufficientFunds
from ledgerline.idempotency import IdempotencyRequest, request_fingerprint
from ledgerline.models import Transaction
from ledgerline.reconcile import reconcile
from ledgerline.repository import LedgerRepository
from ledgerline.schemas import AccountCreate, TransferCreate
from ledgerline.service import LedgerService


def idempotency(body: TransferCreate, key: str | None = None) -> IdempotencyRequest:
    return IdempotencyRequest(
        principal="test-client",
        key=key or str(uuid4()),
        fingerprint=request_fingerprint("POST", "/v1/transfers", body),
    )


def open_wallet(service: LedgerService, name: str, *, allow_negative: bool = False) -> UUID:
    request = AccountCreate(
        name=name, type=AccountType.LIABILITY, currency="USD", allow_negative=allow_negative
    )
    return service.create_account(request, None).body.id


def transfer_request(source: UUID, destination: UUID, amount: int) -> TransferCreate:
    return TransferCreate(
        from_account_id=source, to_account_id=destination, amount=amount, currency="USD"
    )


def funded_wallets(service: LedgerService, amount: int) -> tuple[UUID, UUID]:
    funding = open_wallet(service, "Funding", allow_negative=True)
    source = open_wallet(service, "Source")
    destination = open_wallet(service, "Destination")
    body = transfer_request(funding, source, amount)
    service.transfer(body, idempotency(body))
    return source, destination


def run_simultaneously[T](count: int, task: Callable[[int], T]) -> list[T]:
    """Run ``task`` on ``count`` threads, released together by a barrier."""
    barrier = threading.Barrier(count)

    def wait_then_run(index: int) -> T:
        barrier.wait()
        return task(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(wait_then_run, range(count)))


def assert_reconciles(engine: Engine) -> None:
    with Session(engine) as session:
        report = reconcile(session)
    assert report.ok, report.render()


def count_transactions(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Transaction)) or 0


def test_concurrent_transfers_never_overdraw_or_lose_updates(
    service: LedgerService, db: Engine
) -> None:
    source, _ = funded_wallets(service, 1_000)
    destinations = [open_wallet(service, f"Destination {n}") for n in range(4)]

    def send(index: int) -> str:
        body = transfer_request(source, destinations[index % 4], 30)
        try:
            service.transfer(body, idempotency(body))
        except InsufficientFunds:
            return "insufficient funds"
        except ConcurrentUpdate:
            return "gave up after retries"
        return "ok"

    outcomes = run_simultaneously(40, send)

    succeeded = outcomes.count("ok")
    assert service.get_account(source).balance == 1_000 - 30 * succeeded
    assert service.get_account(source).balance >= 0
    assert sum(service.get_account(d).balance for d in destinations) == 30 * succeeded
    if "gave up after retries" not in outcomes:
        # Then every refusal was a genuine lack of funds: exactly 1000 // 30 fit.
        assert succeeded == 33
    assert_reconciles(db)


def test_concurrent_duplicates_take_effect_exactly_once(service: LedgerService, db: Engine) -> None:
    source, destination = funded_wallets(service, 1_000)
    body = transfer_request(source, destination, 100)
    request = idempotency(body, key="one-payment")
    transactions_before = count_transactions(db)

    results = run_simultaneously(10, lambda _: service.transfer(body, request))

    assert len({result.body.id for result in results}) == 1
    assert [result.replayed for result in results].count(False) == 1
    assert count_transactions(db) == transactions_before + 1
    assert service.get_account(source).balance == 900
    assert service.get_account(destination).balance == 100
    assert_reconciles(db)


def test_compare_and_set_only_succeeds_at_the_expected_version(
    service: LedgerService, db: Engine
) -> None:
    account_id = open_wallet(service, "Wallet")
    with Session(db) as session, session.begin():
        repo = LedgerRepository(session)
        assert not repo.compare_and_set_balance(account_id, expected_version=7, new_balance=500)
        assert repo.compare_and_set_balance(account_id, expected_version=0, new_balance=500)
        # The version is now 1, so a writer that read version 0 loses.
        assert not repo.compare_and_set_balance(account_id, expected_version=0, new_balance=900)
    assert service.get_account(account_id).balance == 500


def test_a_stale_read_is_retried_with_fresh_data(
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
    body = transfer_request(source, destination, 250)
    result = service.transfer(body, idempotency(body))

    assert not result.replayed
    assert service.get_account(source).balance == 750
    assert service.get_account(destination).balance == 250


def test_retries_are_bounded(db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LedgerService(create_session_factory(db), max_attempts=3)
    source, destination = funded_wallets(service, 1_000)
    transactions_before = count_transactions(db)
    attempts = 0

    def always_stale(
        _repo: LedgerRepository, _account_id: UUID, *, expected_version: int, new_balance: int
    ) -> bool:
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(LedgerRepository, "compare_and_set_balance", always_stale)
    body = transfer_request(source, destination, 1)
    with pytest.raises(ConcurrentUpdate):
        service.transfer(body, idempotency(body))

    assert attempts == 3
    assert count_transactions(db) == transactions_before  # every attempt rolled back
    assert service.get_account(source).balance == 1_000


def test_losing_the_idempotency_key_race_rolls_back_and_replays(
    service: LedgerService, db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = funded_wallets(service, 1_000)
    body = transfer_request(source, destination, 100)
    request = idempotency(body, key="raced")
    first = service.transfer(body, request)

    # Simulate a duplicate that started before `first` committed: its initial
    # lookup finds nothing, it does all the work, then loses on the primary key.
    real_lookup = LedgerRepository.get_idempotency_record
    lookups = 0

    def miss_first_lookup(repo: LedgerRepository, principal: str, key: str) -> object:
        nonlocal lookups
        lookups += 1
        return None if lookups == 1 else real_lookup(repo, principal, key)

    monkeypatch.setattr(LedgerRepository, "get_idempotency_record", miss_first_lookup)
    second = service.transfer(body, request)

    assert second.replayed
    assert second.body == first.body
    assert service.get_account(source).balance == 900
    assert_reconciles(db)
