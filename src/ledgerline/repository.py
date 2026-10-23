"""Database access. Every SQL statement the service runs is defined here.

Methods run inside the caller's session and transaction; the service layer
decides where transactions begin and end.
"""

from collections.abc import Collection
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from ledgerline.models import Account, Entry, IdempotencyRecord, Transaction
from ledgerline.pagination import AccountCursor


class DuplicateIdempotencyKey(Exception):
    """A concurrent request committed the same idempotency key first."""


class LedgerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ping(self) -> None:
        self.session.execute(text("SELECT 1"))

    # Accounts

    def add_account(self, account: Account) -> None:
        self.session.add(account)
        self.session.flush()

    def get_account(self, account_id: UUID) -> Account | None:
        return self.session.get(Account, account_id)

    def get_accounts(self, account_ids: Collection[UUID]) -> dict[UUID, Account]:
        accounts = self.session.scalars(select(Account).where(Account.id.in_(account_ids)))
        return {account.id: account for account in accounts}

    def list_accounts(self, *, limit: int, after: AccountCursor | None) -> list[Account]:
        query = select(Account).order_by(Account.created_at, Account.id).limit(limit)
        if after is not None:
            query = query.where(
                or_(
                    Account.created_at > after.created_at,
                    and_(Account.created_at == after.created_at, Account.id > after.id),
                )
            )
        return list(self.session.scalars(query))

    def compare_and_set_balance(
        self, account_id: UUID, *, expected_version: int, new_balance: int
    ) -> bool:
        """Store a new balance only if the account is still at ``expected_version``.

        This is the optimistic-concurrency check: if another transaction changed
        the account after we read it, no row matches and the caller retries with
        fresh data. On PostgreSQL a concurrent, uncommitted update makes this
        statement wait for that transaction, then re-check the WHERE clause.
        """
        result = self.session.connection().execute(
            update(Account)
            .where(Account.id == account_id, Account.version == expected_version)
            .values(balance=new_balance, version=expected_version + 1)
        )
        return result.rowcount == 1

    def sum_entries(self, account_id: UUID, *, posted_at_or_before: datetime) -> int:
        """Signed sum (debits positive) of an account's entries up to a point in time."""
        total = self.session.scalar(
            select(func.coalesce(func.sum(Entry.amount), 0))
            .join(Transaction, Transaction.id == Entry.transaction_id)
            .where(Entry.account_id == account_id, Transaction.posted_at <= posted_at_or_before)
        )
        # PostgreSQL returns SUM(bigint) as NUMERIC, i.e. a Decimal.
        return int(total or 0)

    def list_entries(self, account_id: UUID, *, limit: int, after_id: int | None) -> list[Entry]:
        query = (
            select(Entry)
            .options(joinedload(Entry.transaction))
            .where(Entry.account_id == account_id)
            .order_by(Entry.id)
            .limit(limit)
        )
        if after_id is not None:
            query = query.where(Entry.id > after_id)
        return list(self.session.scalars(query))

    # Transactions

    def add_transaction(self, transaction: Transaction) -> None:
        self.session.add(transaction)
        self.session.flush()

    def get_transaction(self, transaction_id: UUID) -> Transaction | None:
        return self.session.get(
            Transaction, transaction_id, options=[selectinload(Transaction.entries)]
        )

    def find_reversal_id(self, transaction_id: UUID) -> UUID | None:
        return self.session.scalar(
            select(Transaction.id).where(Transaction.reverses_id == transaction_id)
        )

    # Idempotency keys

    def get_idempotency_record(self, principal: str, key: str) -> IdempotencyRecord | None:
        return self.session.get(IdempotencyRecord, (principal, key))

    def add_idempotency_record(self, record: IdempotencyRecord) -> None:
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # The primary key (principal, key) is the arbiter between concurrent
            # requests that both found no record when they started.
            raise DuplicateIdempotencyKey from exc
