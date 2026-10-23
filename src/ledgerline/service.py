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
    apply_entries,
    balance_change,
    check_currencies,
    default_normal_balance,
    reversal_entries,
    transfer_entries,
    validate_entries,
)
from ledgerline.errors import (
    AccountNotFound,
    AlreadyReversed,
    ConcurrentUpdate,
    DatabaseUnavailable,
    IdempotencyKeyReused,
    ReversalNotAllowed,
    TransactionNotFound,
)
from ledgerline.idempotency import IdempotencyRequest
from ledgerline.models import Account, Entry, IdempotencyRecord, Transaction
from ledgerline.pagination import AccountCursor, EntryCursor
from ledgerline.repository import DuplicateIdempotencyKey, LedgerRepository
from ledgerline.schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    EntryOut,
    Page,
    ReversalCreate,
    StatementLine,
    TransactionCreate,
    TransactionOut,
    TransferCreate,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class WriteResult[BodyT: BaseModel]:
    body: BodyT
    replayed: bool
    """True when the body is the stored response of an earlier request with the same key."""


class _StaleBalance(Exception):
    """An account changed between our read and our compare-and-swap."""


class LedgerService:
    def __init__(
        self, session_factory: sessionmaker[Session], *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    # Reads

    def check_database(self) -> None:
        try:
            with self._session_factory() as session:
                LedgerRepository(session).ping()
        except SQLAlchemyError as exc:
            raise DatabaseUnavailable("the database did not answer a health query") from exc

    def get_account(self, account_id: UUID) -> AccountOut:
        with self._session_factory() as session:
            return _account_out(_require_account(LedgerRepository(session), account_id))

    def list_accounts(self, *, limit: int, cursor: str | None) -> Page[AccountOut]:
        after = AccountCursor.decode(cursor) if cursor else None
        with self._session_factory() as session:
            # Fetch one extra row to learn whether there is another page.
            accounts = LedgerRepository(session).list_accounts(limit=limit + 1, after=after)
            page = [_account_out(account) for account in accounts[:limit]]
        next_cursor = None
        if len(accounts) > limit:
            next_cursor = AccountCursor(created_at=page[-1].created_at, id=page[-1].id).encode()
        return Page[AccountOut](data=page, next_cursor=next_cursor)

    def get_balance(self, account_id: UUID, *, as_of: datetime | None) -> BalanceOut:
        with self._session_factory() as session:
            repo = LedgerRepository(session)
            account = _require_account(repo, account_id)
            if as_of is None:
                balance = account.balance
            else:
                # History is rebuilt from the immutable entries, not the cached balance.
                signed_total = repo.sum_entries(account_id, posted_at_or_before=as_of)
                balance = balance_change(account.normal_balance, signed_total)
            return BalanceOut(
                account_id=account.id,
                currency=account.currency,
                normal_balance=account.normal_balance,
                balance=balance,
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
                created_at=_now(),
            )
            repo.add_account(account)
            return _account_out(account)

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
            accounts = _load_accounts(repo, [draft.account_id for draft in drafts])
            return self._post(
                repo, drafts, accounts, description=request.description, metadata=request.metadata
            )

        return self._write(idempotency, TransactionOut, operation)

    def transfer(
        self, request: TransferCreate, idempotency: IdempotencyRequest
    ) -> WriteResult[TransactionOut]:
        def operation(repo: LedgerRepository) -> TransactionOut:
            accounts = _load_accounts(repo, [request.from_account_id, request.to_account_id])
            drafts = transfer_entries(
                accounts[request.from_account_id],
                accounts[request.to_account_id],
                request.amount,
                request.currency,
            )
            return self._post(
                repo, drafts, accounts, description=request.description, metadata=request.metadata
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
            existing = repo.find_reversal_id(transaction_id)
            if existing is not None:
                raise AlreadyReversed(transaction_id, existing)
            drafts = reversal_entries(
                [
                    EntryDraft(account_id=e.account_id, amount=e.amount, currency=e.currency)
                    for e in original.entries
                ]
            )
            accounts = _load_accounts(repo, [draft.account_id for draft in drafts])
            return self._post(
                repo,
                drafts,
                accounts,
                description=request.description or f"Reversal of {transaction_id}",
                metadata=request.metadata,
                reverses_id=transaction_id,
            )

        return self._write(idempotency, TransactionOut, operation)

    def _post(
        self,
        repo: LedgerRepository,
        drafts: Sequence[EntryDraft],
        accounts: dict[UUID, AccountState],
        *,
        description: str | None,
        metadata: dict[str, str],
        reverses_id: UUID | None = None,
    ) -> TransactionOut:
        check_currencies(drafts, accounts)
        new_balances = apply_entries(drafts, accounts)
        # Taking the timestamp after reading the balances means that, per
        # account, posted_at order matches commit order: any transaction this one
        # depended on committed before we read, and therefore before this moment.
        posted_at = _now()
        # Update rows in a fixed order so two transactions touching the same
        # accounts never wait on each other's row locks in a cycle (deadlock).
        for account_id in sorted(new_balances):
            swapped = repo.compare_and_set_balance(
                account_id,
                expected_version=accounts[account_id].version,
                new_balance=new_balances[account_id],
            )
            if not swapped:
                raise _StaleBalance
        transaction = Transaction(
            id=uuid4(),
            description=description,
            metadata_=metadata,
            posted_at=posted_at,
            reverses_id=reverses_id,
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
                                created_at=_now(),
                            )
                        )
                    return WriteResult(body, replayed=False)
            except (_StaleBalance, DuplicateIdempotencyKey) as conflict:
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


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempt: int) -> None:
    # Exponential backoff with full jitter, capped at 50 ms, so that requests
    # which collided once do not retry in lockstep and collide again.
    time.sleep(random.uniform(0, min(0.05, 0.002 * 2**attempt)))  # noqa: S311 - not cryptographic


def _require_account(repo: LedgerRepository, account_id: UUID) -> Account:
    account = repo.get_account(account_id)
    if account is None:
        raise AccountNotFound(account_id)
    return account


def _load_accounts(
    repo: LedgerRepository, account_ids: Collection[UUID]
) -> dict[UUID, AccountState]:
    found = repo.get_accounts(account_ids)
    for account_id in account_ids:
        if account_id not in found:
            raise AccountNotFound(account_id)
    return {
        account.id: AccountState(
            id=account.id,
            currency=account.currency,
            normal_balance=account.normal_balance,
            allow_negative=account.allow_negative,
            balance=account.balance,
            version=account.version,
        )
        for account in found.values()
    }


def _replay[BodyT: BaseModel](
    record: IdempotencyRecord, idempotency: IdempotencyRequest, response_model: type[BodyT]
) -> BodyT:
    if record.fingerprint != idempotency.fingerprint:
        raise IdempotencyKeyReused(
            "this Idempotency-Key was already used for a different request; "
            "use a new key for a new request"
        )
    return response_model.model_validate(record.response_body)


def _account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        name=account.name,
        type=account.type,
        currency=account.currency,
        normal_balance=account.normal_balance,
        allow_negative=account.allow_negative,
        balance=account.balance,
        metadata=account.metadata_,
        created_at=account.created_at,
    )


def _transaction_out(transaction: Transaction, *, reversed_by: UUID | None) -> TransactionOut:
    return TransactionOut(
        id=transaction.id,
        description=transaction.description,
        metadata=transaction.metadata_,
        posted_at=transaction.posted_at,
        reverses_transaction_id=transaction.reverses_id,
        reversed_by_transaction_id=reversed_by,
        entries=[
            EntryOut(account_id=entry.account_id, amount=entry.amount, currency=entry.currency)
            for entry in transaction.entries
        ],
    )
