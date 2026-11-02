"""Pydantic models for request bodies and response payloads.

Request models validate shape and per-field limits (types, formats, sizes);
the accounting rules that span several fields live in ledgerline.domain.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from ledgerline.domain import MAX_MAGNITUDE, AccountType, HoldStatus, NormalBalance

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


def _representable_in_utc(value: datetime) -> datetime:
    # Timestamps are stored and compared in UTC. A valid offset can move an
    # instant near the ends of the calendar out of the years 1 to 9999 there,
    # where Python cannot represent it: reject it here instead of failing later.
    try:
        value.astimezone(UTC)
    except OverflowError:
        raise ValueError("the timestamp is outside the years 1 to 9999 in UTC") from None
    # Return the value as given, so idempotency fingerprints and echoed values keep it.
    return value


AwareTimestamp = Annotated[AwareDatetime, AfterValidator(_representable_in_utc)]
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


class HoldCreate(RequestModel):
    from_account_id: UUID
    to_account_id: UUID
    amount: PositiveAmount
    currency: Currency
    expires_at: AwareTimestamp = Field(
        description="When the hold lapses if it is not captured or voided first. ISO 8601 "
        "with a timezone, later than the current time.",
        examples=["2026-11-08T12:00:00Z"],
    )
    description: Description | None = None
    metadata: Metadata = Field(default_factory=dict)


class HoldCapture(RequestModel):
    amount: PositiveAmount | None = Field(
        default=None,
        description="How much to capture; defaults to the whole hold. Any remainder is released.",
    )
    description: Description | None = None
    metadata: Metadata = Field(default_factory=dict)


_BALANCE = "Ledger balance: posted entries only, in the account's normal-balance terms."
_HELD = "Total of the account's active holds (funds reserved for later transfers)."
_AVAILABLE = "balance minus held_amount: what the account can spend or hold right now."


class AccountOut(BaseModel):
    id: UUID
    name: str
    type: AccountType
    currency: str
    normal_balance: NormalBalance
    allow_negative: bool
    balance: int = Field(description=_BALANCE)
    held_amount: int = Field(description=_HELD)
    available_balance: int = Field(description=_AVAILABLE)
    metadata: dict[str, str]
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _fill_fields_added_with_holds(cls, data: Any) -> Any:
        # Responses stored under an Idempotency-Key before holds existed (0.1.0)
        # lack these fields. Such an account had no holds, so they follow from
        # the balance; a replay of that response must still validate.
        if isinstance(data, dict) and "held_amount" not in data and "balance" in data:
            return {**data, "held_amount": 0, "available_balance": data["balance"]}
        return data


class BalanceOut(BaseModel):
    account_id: UUID
    currency: str
    normal_balance: NormalBalance
    balance: int = Field(description=_BALANCE)
    held_amount: int = Field(description=_HELD)
    available_balance: int = Field(description=_AVAILABLE)
    as_of: datetime | None = Field(description="The point in time requested, or null for now.")


class HoldOut(BaseModel):
    id: UUID
    status: HoldStatus = Field(
        description="active, captured, voided or expired (an active hold past its expires_at)."
    )
    from_account_id: UUID
    to_account_id: UUID
    amount: int = Field(description="The authorized amount.")
    captured_amount: int | None = Field(description="Set once the hold is captured.")
    currency: str
    description: str | None
    metadata: dict[str, str]
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None = Field(description="When the hold was captured or voided.")
    capture_transaction_id: UUID | None = Field(
        description="The journal transaction the capture posted."
    )


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
    # A default, because responses stored under an Idempotency-Key before holds
    # existed (0.1.0) lack the field and must still replay.
    captures_hold_id: UUID | None = Field(
        default=None, description="The hold this transaction captured, if any."
    )
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
