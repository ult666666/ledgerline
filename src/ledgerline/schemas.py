"""Pydantic models for request bodies and response payloads.

Request models validate shape and per-field limits (types, formats, sizes);
the accounting rules that span several fields live in ledgerline.domain.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from ledgerline.domain import MAX_MAGNITUDE, AccountType, NormalBalance

MAX_ENTRIES_PER_TRANSACTION = 100

Currency = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z]{3}$"),
    Field(description="Three-letter ISO 4217 currency code.", examples=["USD"]),
]
# strict=True rejects floats (12.5) and numeric strings ("1250"): money moves
# only as JSON integers of minor units.
SignedAmount = Annotated[
    int,
    Field(
        strict=True,
        ge=-MAX_MAGNITUDE,
        le=MAX_MAGNITUDE,
        description="Integer minor units. Positive is a debit, negative is a credit.",
        examples=[2500],
    ),
]
PositiveAmount = Annotated[
    int,
    Field(strict=True, gt=0, le=MAX_MAGNITUDE, description="Integer minor units.", examples=[2500]),
]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Metadata = Annotated[
    dict[
        Annotated[str, StringConstraints(min_length=1, max_length=40)],
        Annotated[str, StringConstraints(max_length=500)],
    ],
    Field(max_length=20, description="Up to 20 string key/value pairs for your own use."),
]


class RequestModel(BaseModel):
    # Unknown fields are rejected so a typo such as "ammount" fails loudly.
    model_config = ConfigDict(extra="forbid")


class AccountCreate(RequestModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    type: AccountType
    currency: Currency
    normal_balance: NormalBalance | None = Field(
        default=None,
        description="Defaults to debit for asset/expense accounts and credit for the others.",
    )
    allow_negative: StrictBool = Field(
        default=False, description="Whether the balance may go below zero."
    )
    metadata: Metadata = Field(default_factory=dict)


class EntryIn(RequestModel):
    account_id: UUID
    amount: SignedAmount
    currency: Currency


class TransactionCreate(RequestModel):
    description: Description | None = None
    metadata: Metadata = Field(default_factory=dict)
    entries: list[EntryIn] = Field(max_length=MAX_ENTRIES_PER_TRANSACTION)


class TransferCreate(RequestModel):
    from_account_id: UUID
    to_account_id: UUID
    amount: PositiveAmount
    currency: Currency
    description: Description | None = None
    metadata: Metadata = Field(default_factory=dict)


class ReversalCreate(RequestModel):
    description: Description | None = None
    metadata: Metadata = Field(default_factory=dict)


class AccountOut(BaseModel):
    id: UUID
    name: str
    type: AccountType
    currency: str
    normal_balance: NormalBalance
    allow_negative: bool
    balance: int = Field(description="Current balance in the account's normal-balance terms.")
    metadata: dict[str, str]
    created_at: datetime


class BalanceOut(BaseModel):
    account_id: UUID
    currency: str
    normal_balance: NormalBalance
    balance: int
    as_of: datetime | None = Field(description="The point in time requested, or null for now.")


class EntryOut(BaseModel):
    account_id: UUID
    amount: int
    currency: str


class TransactionOut(BaseModel):
    id: UUID
    description: str | None
    metadata: dict[str, str]
    posted_at: datetime
    reverses_transaction_id: UUID | None
    reversed_by_transaction_id: UUID | None
    entries: list[EntryOut]


class StatementLine(BaseModel):
    transaction_id: UUID
    posted_at: datetime
    description: str | None
    amount: int
    currency: str
    balance_after: int = Field(description="The account's balance right after this entry.")


class Page[ItemT](BaseModel):
    data: list[ItemT]
    next_cursor: str | None = Field(description="Pass as ?cursor= to fetch the next page.")
