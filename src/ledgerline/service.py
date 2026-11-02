"""Ledger operations: the business rules and the transaction boundaries around them.

Every write runs in a single database transaction that

1. looks up the idempotency key and, if it was already used, replays the stored
   response (or rejects a different request reusing the key);
2. validates the posting against the current balances;
3. compare-and-swaps each touched account's stored balance (optimistic
   concurrency on a version column);
4. inserts the journal transaction and its entries; and
5. records the idempotency key together with the response.

Either all of that commits or none of it does. If another request changed one
of the accounts (or claimed the same key) in the meantime, the attempt is
rolled back and retried with fresh data, a bounded number of times.

Holds use the same path. Authorizing or voiding a hold changes no balance, but
it bumps the source account's version all the same, so every write that can
change an account's available balance goes through that account's
compare-and-swap.
"""

import logging
import random
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ledgerline.domain import (
    AccountState,
    EntryDraft,
    HoldState,
    HoldStatus,
    apply_entries,
    balance_change,
    capture_amount,
    check_currencies,
    check_hold_request,
    default_normal_balance,
    hold_status,
    require_active,
    reversal_entries,
    transfer_entries,
    validate_entries,
)
from ledgerline.errors import (
    AccountNotFound,
    AlreadyReversed,
    ConcurrentUpdate,
    DatabaseUnavailable,
    HoldNotFound,
    IdempotencyKeyReused,
    ReversalNotAllowed,
    TransactionNotFound,
)
from ledgerline.idempotency import IdempotencyRequest
from ledgerline.models import Account, Entry, Hold, IdempotencyRecord, Transaction
from ledgerline.pagination import AccountCursor, EntryCursor
from ledgerline.repository import DuplicateIdempotencyKey, LedgerRepository
from ledgerline.schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    EntryOut,
    HoldCapture,
    HoldCreate,
    HoldOut,
    Page,
    ReversalCreate,
    StatementLine,
    TransactionCreate,
    TransactionOut,
    TransferCreate,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 10

Clock = Callable[[], datetime]
"""Returns the current time, timezone-aware. Injectable so tests control expiry."""


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WriteResult[BodyT: BaseModel]:
    body: BodyT
    replayed: bool
    """True when the body is the stored response of an earlier request with the same key."""


class _StaleBalance(Exception):
    """An account changed between our read and our compare-and-swap."""


class _StaleHold(Exception):
    """A hold was captured or voided between our read and our state transition."""


class LedgerService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._clock = clock

    # Reads

    def check_database(self) -> None:
        try:
            with self._session_factory() as session:
                LedgerRepository(session).ping()
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("the database did not answer a health query") from exc

    def get_account(self, account_id: UUID) -> AccountOut:
        with self._session_factory() as session:
            found = LedgerRepository(session).get_account_with_held(account_id, now=self._clock())
            if found is None:
                raise AccountNotFound(account_id)
            return _account_out(*found)

    def list_accounts(self, *, limit: int, cursor: str | None) -> Page[AccountOut]:
        after = AccountCursor.decode(cursor) if cursor else None
        with self._session_factory() as session:
            # Fetch one extra row to learn whether there is another page.
            rows = LedgerRepository(session).list_accounts(
                limit=limit + 1, after=after, now=self._clock()
            )
            page = [_account_out(account, held) for account, held in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = AccountCursor(created_at=page[-1].created_at, id=page[-1].id).encode()
        return Page[AccountOut](data=page, next_cursor=next_cursor)

    def get_balance(self, account_id: UUID, *, as_of: datetime | None) -> BalanceOut:
        with self._session_factory() as session:
            repo = LedgerRepository(session)
            if as_of is None:
                found = repo.get_account_with_held(account_id, now=self._clock())
                if found is None:
                    raise AccountNotFound(account_id)
                account, held = found
                balance = account.balance
            else:
                # History is rebuilt from the immutable entries and the hold
                # timestamps, not from the cached balance.
                account = _require_account(repo, account_id)
                signed_total = repo.sum_entries(account_id, posted_at_or_before=as_of)
                balance = balance_change(account.normal_balance, signed_total)
                held = repo.sum_holds(account_id, active_at=as_of)
            return BalanceOut(
                account_id=account.id,
                currency=account.currency,
                normal_balance=account.normal_balance,
                balance=balance,
                held_amount=held,
                available_balance=balance - held,
                as_of=as_of,
            )

    def list_entries(
        self, account_id: UUID, *, limit: int, cursor: str | None
    ) -> Page[StatementLine]:
        after = EntryCursor.decode(cursor) if cursor else None
        with self._session_factory() as session:
            repo = LedgerRepository(session)
            _require_account(repo, account_id)
            entries = repo.list_entries(
                account_id, limit=limit + 1, after_id=after.entry_id if after else None
            )
            page = entries[:limit]
            lines = [
                StatementLine(
                    transaction_id=entry.transaction_id,
                    posted_at=entry.transaction.posted_at,
                    description=entry.transaction.description,
                    amount=entry.amount,
                    currency=entry.currency,
                    balance_after=entry.balance_after,
                )
                for entry in page
            ]
        next_cursor = None
        if len(entries) > limit:
            next_cursor = EntryCursor(entry_id=page[-1].id).encode()
        return Page[StatementLine](data=lines, next_cursor=next_cursor)

    def get_transaction(self, transaction_id: UUID) -> TransactionOut:
        with self._session_factory() as session:
            repo = LedgerRepository(session)
            transaction = repo.get_transaction(transaction_id)
            if transaction is None:
                raise TransactionNotFound(transaction_id)
            return _transaction_out(transaction, reversed_by=repo.find_reversal_id(transaction_id))

    def get_hold(self, hold_id: UUID) -> HoldOut:
        with self._session_factory() as session:
            repo = LedgerRepository(session)
            hold = _require_hold(repo, hold_id)
            return _hold_out(hold, now=self._clock(), capture_id=repo.find_capture_id(hold_id))

    # Writes

    def create_account(
        self, request: AccountCreate, idempotency: IdempotencyRequest | None
    ) -> WriteResult[AccountOut]:
        def operation(repo: LedgerRepository) -> AccountOut:
            account = Account(
                id=uuid4(),
                name=request.name,
                type=request.type,
                currency=request.currency,
                normal_balance=request.normal_balance or default_normal_balance(request.type),
                allow_negative=request.allow_negative,
                metadata_=request.metadata,
                balance=0,
                version=0,
                created_at=self._clock(),
            )
            repo.add_account(account)
            return _account_out(account, held=0)

        return self._write(idempotency, AccountOut, operation)

    def post_transaction(
        self, request: TransactionCreate, idempotency: IdempotencyRequest
    ) -> WriteResult[TransactionOut]:
        drafts = [
            EntryDraft(account_id=entry.account_id, amount=entry.amount, currency=entry.currency)
            for entry in request.entries
        ]
        # Fail fast on malformed postings before opening a database transaction.
        validate_entries(drafts)

        def operation(repo: LedgerRepository) -> TransactionOut:
            accounts, now = self._load_accounts(repo, [draft.account_id for draft in drafts])
            return self._post(
                repo,
                drafts,
                accounts,
                posted_at=now,
                description=request.description,
                metadata=request.metadata,
            )

        return self._write(idempotency, TransactionOut, operation)

    def transfer(
        self, request: TransferCreate, idempotency: IdempotencyRequest
    ) -> WriteResult[TransactionOut]:
        def operation(repo: LedgerRepository) -> TransactionOut:
            accounts, now = self._load_accounts(
                repo, [request.from_account_id, request.to_account_id]
            )
            drafts = transfer_entries(
                accounts[request.from_account_id],
                accounts[request.to_account_id],
                request.amount,
                request.currency,
            )
            return self._post(
                repo,
                drafts,
                accounts,
                posted_at=now,
                description=request.description,
                metadata=request.metadata,
            )

        return self._write(idempotency, TransactionOut, operation)

    def reverse_transaction(
        self, transaction_id: UUID, request: ReversalCreate, idempotency: IdempotencyRequest
    ) -> WriteResult[TransactionOut]:
        def operation(repo: LedgerRepository) -> TransactionOut:
            original = repo.get_transaction(transaction_id)
            if original is None:
                raise TransactionNotFound(transaction_id)
            if original.reverses_id is not None:
                raise ReversalNotAllowed(
                    f"transaction {transaction_id} is itself a reversal; "
                    "post a new transaction to redo the original",
                    transaction_id=str(transaction_id),
                )
            drafts = reversal_entries(
                [
                    EntryDraft(account_id=e.account_id, amount=e.amount, currency=e.currency)
                    for e in original.entries
                ]
            )
            accounts, now = self._load_accounts(repo, [draft.account_id for draft in drafts])
            # Look for an earlier reversal only after reading the balances. On
            # PostgreSQL each statement sees what had committed when it started:
            # a rival reversal that committed before the balance read shows up
            # here, and one that commits later changes the balances, so the
            # compare-and-swap fails and the attempt is retried.
            existing = repo.find_reversal_id(transaction_id)
            if existing is not None:
                raise AlreadyReversed(transaction_id, existing)
            return self._post(
                repo,
                drafts,
                accounts,
                posted_at=now,
                description=request.description or f"Reversal of {transaction_id}",
                metadata=request.metadata,
                reverses_id=transaction_id,
            )

        return self._write(idempotency, TransactionOut, operation)

    def create_hold(
        self, request: HoldCreate, idempotency: IdempotencyRequest
    ) -> WriteResult[HoldOut]:
        """Reserve funds on the source account without posting anything (authorization)."""

        def operation(repo: LedgerRepository) -> HoldOut:
            accounts, now = self._load_accounts(
                repo, [request.from_account_id, request.to_account_id]
            )
            source = accounts[request.from_account_id]
            check_hold_request(
                source,
                accounts[request.to_account_id],
                request.amount,
                request.currency,
                expires_at=request.expires_at,
                now=now,
            )
            # No balance changes, but the available balance does: bumping the
            # version makes concurrent holds and postings on this account
            # serialize through the compare-and-swap.
            _bump_version(repo, source)
            hold = Hold(
                id=uuid4(),
                from_account_id=request.from_account_id,
                to_account_id=request.to_account_id,
                amount=request.amount,
                currency=request.currency,
                status=HoldStatus.ACTIVE,
                captured_amount=None,
                description=request.description,
                metadata_=request.metadata,
                created_at=now,
                expires_at=request.expires_at,
                closed_at=None,
            )
            repo.add_hold(hold)
            return _hold_out(hold, now=now, capture_id=None)

        return self._write(idempotency, HoldOut, operation)

    def capture_hold(
        self, hold_id: UUID, request: HoldCapture, idempotency: IdempotencyRequest
    ) -> WriteResult[HoldOut]:
        """Post the hold's transfer, for all of it or less, and release any remainder."""

        def operation(repo: LedgerRepository) -> HoldOut:
            hold = _require_hold(repo, hold_id)
            # The hold being captured no longer counts against the source: its
            # funds become the posting itself, and the remainder is released.
            accounts, now = self._load_accounts(
                repo, [hold.from_account_id, hold.to_account_id], excluding_hold=hold.id
            )
            state = _hold_state(hold)
            require_active(state, now)
            amount = capture_amount(state, request.amount)
            drafts = transfer_entries(
                accounts[hold.from_account_id], accounts[hold.to_account_id], amount, hold.currency
            )
            transaction = self._post(
                repo,
                drafts,
                accounts,
                posted_at=now,
                description=request.description or f"Capture of hold {hold.id}",
                metadata=request.metadata,
                capture=(hold, amount),
            )
            return _hold_out(hold, now=now, capture_id=transaction.id)

        return self._write(idempotency, HoldOut, operation)

    def void_hold(self, hold_id: UUID, idempotency: IdempotencyRequest) -> WriteResult[HoldOut]:
        """Release a hold's funds without posting anything."""

        def operation(repo: LedgerRepository) -> HoldOut:
            hold = _require_hold(repo, hold_id)
            accounts, now = self._load_accounts(repo, [hold.from_account_id])
            require_active(_hold_state(hold), now)
            _bump_version(repo, accounts[hold.from_account_id])
            if not repo.close_hold(hold, status=HoldStatus.VOIDED, at=now):
                raise _StaleHold
            return _hold_out(hold, now=now, capture_id=None)

        return self._write(idempotency, HoldOut, operation)

    def _load_accounts(
        self,
        repo: LedgerRepository,
        account_ids: Collection[UUID],
        *,
        excluding_hold: UUID | None = None,
    ) -> tuple[dict[UUID, AccountState], datetime]:
        """Read the accounts, then the clock, then the accounts' active holds.

        The returned time is taken after the account rows are read, which keeps
        each account's posted_at (and hold timestamps) in commit order: anything
        this write depends on committed before the read, so before this moment.
        The holds are read at that same moment, so the expiry decision and the
        recorded timestamp agree.
        """
        found = repo.get_accounts(account_ids)
        for account_id in account_ids:
            if account_id not in found:
                raise AccountNotFound(account_id)
        now = self._clock()
        held = repo.held_amounts(account_ids, now=now, excluding=excluding_hold)
        states = {
            account.id: AccountState(
                id=account.id,
                currency=account.currency,
                normal_balance=account.normal_balance,
                allow_negative=account.allow_negative,
                balance=account.balance,
                version=account.version,
                held=held.get(account.id, 0),
            )
            for account in found.values()
        }
        return states, now

    def _post(
        self,
        repo: LedgerRepository,
        drafts: Sequence[EntryDraft],
        accounts: dict[UUID, AccountState],
        *,
        posted_at: datetime,
        description: str | None,
        metadata: dict[str, str],
        reverses_id: UUID | None = None,
        capture: tuple[Hold, int] | None = None,
    ) -> TransactionOut:
        """Post ``drafts``; with ``capture=(hold, amount)`` the posting captures that hold."""
        check_currencies(drafts, accounts)
        new_balances = apply_entries(drafts, accounts)
        # Update rows in a fixed order so two transactions touching the same
        # accounts never wait on each other's row locks in a cycle (deadlock).
        # Holds come after accounts in that order.
        for account_id in sorted(new_balances):
            swapped = repo.compare_and_set_balance(
                account_id,
                expected_version=accounts[account_id].version,
                new_balance=new_balances[account_id],
            )
            if not swapped:
                raise _StaleBalance
        if capture is not None:
            hold, amount = capture
            # Close the hold before inserting anything. On PostgreSQL a rival
            # capture or void can commit between our read of the hold and our
            # read of the accounts; our compare-and-swap then passes, so the
            # guarded transition is what refuses this attempt (it retries and
            # reports the hold as closed). Inserting first would trip the
            # UNIQUE hold_id constraint instead.
            if not repo.close_hold(
                hold, status=HoldStatus.CAPTURED, at=posted_at, captured_amount=amount
            ):
                raise _StaleHold
        transaction = Transaction(
            id=uuid4(),
            description=description,
            metadata_=metadata,
            posted_at=posted_at,
            reverses_id=reverses_id,
            hold_id=None if capture is None else capture[0].id,
            entries=[
                Entry(
                    account_id=draft.account_id,
                    amount=draft.amount,
                    currency=draft.currency,
                    balance_after=new_balances[draft.account_id],
                )
                for draft in drafts
            ],
        )
        repo.add_transaction(transaction)
        return _transaction_out(transaction, reversed_by=None)

    def _write[BodyT: BaseModel](
        self,
        idempotency: IdempotencyRequest | None,
        response_model: type[BodyT],
        operation: Callable[[LedgerRepository], BodyT],
    ) -> WriteResult[BodyT]:
        """Run ``operation`` in its own database transaction, idempotently, with retries."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                with self._session_factory.begin() as session:
                    repo = LedgerRepository(session)
                    if idempotency is not None:
                        record = repo.get_idempotency_record(idempotency.principal, idempotency.key)
                        if record is not None:
                            return WriteResult(
                                _replay(record, idempotency, response_model), replayed=True
                            )
                    body = operation(repo)
                    if idempotency is not None:
                        repo.add_idempotency_record(
                            IdempotencyRecord(
                                principal=idempotency.principal,
                                key=idempotency.key,
                                fingerprint=idempotency.fingerprint,
                                response_body=body.model_dump(mode="json"),
                                created_at=self._clock(),
                            )
                        )
                    return WriteResult(body, replayed=False)
            except (_StaleBalance, _StaleHold, DuplicateIdempotencyKey) as conflict:
                logger.info(
                    "write conflict",
                    extra={"fields": {"attempt": attempt, "reason": type(conflict).__name__}},
                )
                if attempt < self._max_attempts:
                    _backoff(attempt)
        raise ConcurrentUpdate(
            "the accounts involved kept changing under concurrent requests; "
            "retry with the same Idempotency-Key",
            attempts=self._max_attempts,
        )


def _backoff(attempt: int) -> None:
    # Exponential backoff with full jitter, capped at 50 ms, so that requests
    # which collided once do not retry in lockstep and collide again.
    time.sleep(random.uniform(0, min(0.05, 0.002 * 2**attempt)))  # noqa: S311 - not cryptographic


def _require_account(repo: LedgerRepository, account_id: UUID) -> Account:
    account = repo.get_account(account_id)
    if account is None:
        raise AccountNotFound(account_id)
    return account


def _require_hold(repo: LedgerRepository, hold_id: UUID) -> Hold:
    hold = repo.get_hold(hold_id)
    if hold is None:
        raise HoldNotFound(hold_id)
    return hold


def _bump_version(repo: LedgerRepository, account: AccountState) -> None:
    """Compare-and-swap an account's version without changing its balance."""
    if not repo.compare_and_set_balance(
        account.id, expected_version=account.version, new_balance=account.balance
    ):
        raise _StaleBalance


def _hold_state(hold: Hold) -> HoldState:
    return HoldState(
        id=hold.id, status=HoldStatus(hold.status), amount=hold.amount, expires_at=hold.expires_at
    )


def _replay[BodyT: BaseModel](
    record: IdempotencyRecord, idempotency: IdempotencyRequest, response_model: type[BodyT]
) -> BodyT:
    if record.fingerprint != idempotency.fingerprint:
        raise IdempotencyKeyReused(
            "this Idempotency-Key was already used for a different request; "
            "use a new key for a new request"
        )
    return response_model.model_validate(record.response_body)


def _account_out(account: Account, held: int) -> AccountOut:
    return AccountOut(
        id=account.id,
        name=account.name,
        type=account.type,
        currency=account.currency,
        normal_balance=account.normal_balance,
        allow_negative=account.allow_negative,
        balance=account.balance,
        held_amount=held,
        available_balance=account.balance - held,
        metadata=account.metadata_,
        created_at=account.created_at,
    )


def _hold_out(hold: Hold, *, now: datetime, capture_id: UUID | None) -> HoldOut:
    return HoldOut(
        id=hold.id,
        status=hold_status(HoldStatus(hold.status), hold.expires_at, now),
        from_account_id=hold.from_account_id,
        to_account_id=hold.to_account_id,
        amount=hold.amount,
        captured_amount=hold.captured_amount,
        currency=hold.currency,
        description=hold.description,
        metadata=hold.metadata_,
        created_at=hold.created_at,
        expires_at=hold.expires_at,
        closed_at=hold.closed_at,
        capture_transaction_id=capture_id,
    )


def _transaction_out(transaction: Transaction, *, reversed_by: UUID | None) -> TransactionOut:
    return TransactionOut(
        id=transaction.id,
        description=transaction.description,
        metadata=transaction.metadata_,
        posted_at=transaction.posted_at,
        reverses_transaction_id=transaction.reverses_id,
        reversed_by_transaction_id=reversed_by,
        captures_hold_id=transaction.hold_id,
        entries=[
            EntryOut(account_id=entry.account_id, amount=entry.amount, currency=entry.currency)
            for entry in transaction.entries
        ],
    )
