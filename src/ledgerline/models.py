"""SQLAlchemy ORM models: the ledger's storage schema.

The constraints here are a second line of defence behind the service layer:
even a buggy code path cannot store a zero-amount entry, list the same account
twice in one transaction, reverse a transaction twice or overdraw an account
that does not allow negative balances.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ledgerline.domain import AccountType, NormalBalance

# Deterministic constraint names keep Alembic migrations stable across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps on every backend.

    PostgreSQL stores ``timestamptz`` natively, but SQLite has no timezone-aware
    type and hands back naive datetimes. Normalising to UTC on the way in and
    re-attaching UTC on the way out gives the rest of the code one behaviour.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are ambiguous; pass a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enum_values(enum_class: type[AccountType] | type[NormalBalance]) -> list[str]:
    # Store "asset" rather than the Python member name "ASSET".
    return [member.value for member in enum_class]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Defence in depth for the overdraft rule enforced in the service layer.
        CheckConstraint("allow_negative OR balance >= 0", name="balance_not_negative"),
        # Keyset pagination orders accounts by (created_at, id).
        Index(None, "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[AccountType] = mapped_column(
        Enum(
            AccountType,
            name="account_type",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=_enum_values,
        )
    )
    currency: Mapped[str] = mapped_column(String(3))
    normal_balance: Mapped[NormalBalance] = mapped_column(
        Enum(
            NormalBalance,
            name="normal_balance",
            native_enum=False,
            create_constraint=True,
            length=6,
            values_callable=_enum_values,
        )
    )
    allow_negative: Mapped[bool]
    # "metadata" is reserved on declarative classes, hence the trailing underscore.
    metadata_: Mapped[dict[str, str]] = mapped_column("metadata", JSON)
    # Cached balance in the account's own terms (see ledgerline.domain); always
    # equal to the sum of its entries, which `ledgerline reconcile` verifies.
    balance: Mapped[int] = mapped_column(BigInteger)
    # Incremented on every balance change; writers compare-and-swap on it.
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Transaction(Base):
    """An immutable journal transaction. Corrections are new (reversal) transactions."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    description: Mapped[str | None] = mapped_column(String(500))
    metadata_: Mapped[dict[str, str]] = mapped_column("metadata", JSON)
    posted_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # UNIQUE: a transaction can be reversed at most once.
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), unique=True
    )

    entries: Mapped[list["Entry"]] = relationship(back_populates="transaction", order_by="Entry.id")


class Entry(Base):
    """One leg of a transaction: a signed amount (debit > 0, credit < 0) on one account."""

    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_not_zero"),
        UniqueConstraint("transaction_id", "account_id"),
        # Account statements and point-in-time balances scan entries per account.
        Index(None, "account_id", "id"),
    )

    # SQLite only auto-increments INTEGER PRIMARY KEY columns, so use INTEGER
    # there and BIGINT everywhere else.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    # The account's balance right after this entry, for statements.
    balance_after: Mapped[int] = mapped_column(BigInteger)

    transaction: Mapped[Transaction] = relationship(back_populates="entries")


class IdempotencyRecord(Base):
    """The stored outcome of a write made with an Idempotency-Key header."""

    __tablename__ = "idempotency_keys"

    # Keys are scoped per API client so two clients cannot collide (or read
    # each other's responses) by choosing the same key.
    principal: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # SHA-256 of method, path and canonical request body.
    fingerprint: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
