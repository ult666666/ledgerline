"""Double-entry rules as pure functions: no database, no HTTP.

Sign convention: an entry's ``amount`` is in integer minor units (cents for
USD) and is *signed* - positive for a debit, negative for a credit. A journal
transaction is balanced when, for every currency, its amounts sum to zero
(total debits == total credits).

Balances are reported in each account's own terms ("normal balance"): an asset
account holding $100 has balance 10000, and so does a customer wallet (a
liability) that owes the customer $100, even though the first was built from
debits and the second from credits.

A hold reserves part of an account's balance for a later transfer (a two-phase
transfer). The *ledger balance* counts posted entries only; the *available
balance* is the ledger balance minus the account's active holds, and it is what
an account that may not go negative is allowed to spend.
"""

import enum
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ledgerline.errors import (
    HoldExpired,
    HoldNotActive,
    InsufficientFunds,
    InvalidHold,
    InvalidTransaction,
    InvalidTransfer,
    UnbalancedTransaction,
    timestamp,
)

# JavaScript clients parse JSON numbers as IEEE-754 doubles, which represent
# integers exactly only up to 2**53 - 1. Keeping every amount and balance inside
# that range means no client silently rounds money, and it fits in a BIGINT.
MAX_MAGNITUDE = 2**53 - 1


class AccountType(enum.StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class NormalBalance(enum.StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


def default_normal_balance(account_type: AccountType) -> NormalBalance:
    """Assets and expenses grow with debits; liabilities, equity and revenue with credits."""
    if account_type in (AccountType.ASSET, AccountType.EXPENSE):
        return NormalBalance.DEBIT
    return NormalBalance.CREDIT


def balance_change(normal_balance: NormalBalance, amount: int) -> int:
    """How a signed entry (debit > 0, credit < 0) moves a balance in the account's own terms."""
    return amount if normal_balance is NormalBalance.DEBIT else -amount


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """One proposed leg of a journal transaction."""

    account_id: UUID
    amount: int
    currency: str


class HoldStatus(enum.StrEnum):
    """A hold's state as the API reports it.

    Only ``active``, ``captured`` and ``voided`` are ever stored. Expiry is lazy:
    an active hold whose ``expires_at`` has passed is reported as ``expired`` and
    stops counting against the available balance, without any write.
    """

    ACTIVE = "active"
    CAPTURED = "captured"
    VOIDED = "voided"
    EXPIRED = "expired"


STORED_HOLD_STATUSES = (HoldStatus.ACTIVE, HoldStatus.CAPTURED, HoldStatus.VOIDED)

# The hold state machine. Every hold starts active; the other states are final.
HOLD_TRANSITIONS: Mapping[HoldStatus, frozenset[HoldStatus]] = {
    HoldStatus.ACTIVE: frozenset({HoldStatus.CAPTURED, HoldStatus.VOIDED, HoldStatus.EXPIRED}),
    HoldStatus.CAPTURED: frozenset(),
    HoldStatus.VOIDED: frozenset(),
    HoldStatus.EXPIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the posting rules need to know about an account, detached from the ORM."""

    id: UUID
    currency: str
    normal_balance: NormalBalance
    allow_negative: bool
    balance: int
    version: int
    held: int = 0
    """Total of the account's active holds; the available balance is ``balance - held``."""

    @property
    def available(self) -> int:
        return self.balance - self.held


@dataclass(frozen=True, slots=True)
class HoldState:
    """What the hold rules need to know about a hold, detached from the ORM."""

    id: UUID
    status: HoldStatus
    """The stored status: active, captured or voided."""
    amount: int
    expires_at: datetime


def validate_entries(entries: Sequence[EntryDraft]) -> None:
    """Check the rules every journal transaction must satisfy, before touching the database."""
    if len(entries) < 2:
        raise InvalidTransaction("a transaction needs at least two entries")
    seen: set[UUID] = set()
    totals: dict[str, int] = defaultdict(int)
    for position, entry in enumerate(entries):
        if entry.amount == 0:
            raise InvalidTransaction(f"entries[{position}]: amount must not be zero")
        if abs(entry.amount) > MAX_MAGNITUDE:
            raise InvalidTransaction(
                f"entries[{position}]: amount must be between -{MAX_MAGNITUDE} and {MAX_MAGNITUDE}"
            )
        # One leg per account keeps every intermediate balance meaningful and
        # gives each statement line a single, well-defined balance_after.
        if entry.account_id in seen:
            raise InvalidTransaction(
                f"entries[{position}]: account {entry.account_id} appears more than once; "
                "combine its legs into one entry"
            )
        seen.add(entry.account_id)
        totals[entry.currency] += entry.amount

    imbalances = {currency: total for currency, total in sorted(totals.items()) if total != 0}
    if imbalances:
        summary = ", ".join(f"{currency} {total:+d}" for currency, total in imbalances.items())
        raise UnbalancedTransaction(
            f"entries must sum to zero for each currency (off by {summary})",
            imbalances=imbalances,
        )


def check_currencies(entries: Sequence[EntryDraft], accounts: Mapping[UUID, AccountState]) -> None:
    """Every entry must be in its account's currency; the ledger never converts money."""
    for position, entry in enumerate(entries):
        account = accounts[entry.account_id]
        if entry.currency != account.currency:
            raise InvalidTransaction(
                f"entries[{position}]: currency {entry.currency} does not match account "
                f"{account.id}, which is denominated in {account.currency}"
            )


def apply_entries(
    entries: Sequence[EntryDraft], accounts: Mapping[UUID, AccountState]
) -> dict[UUID, int]:
    """Return the balance each touched account would have after posting ``entries``.

    Raises InsufficientFunds if an account that does not allow negative balances
    would drop below zero, counting its active holds: such an account may spend
    only its available balance, so posted entries can never take funds that a
    hold has reserved.
    """
    new_balances: dict[UUID, int] = {}
    for entry in entries:
        account = accounts[entry.account_id]
        current = new_balances.get(account.id, account.balance)
        change = balance_change(account.normal_balance, entry.amount)
        updated = current + change
        if updated - account.held < 0 and not account.allow_negative:
            raise InsufficientFunds(account.id, balance=current, change=change, held=account.held)
        if abs(updated) > MAX_MAGNITUDE:
            raise InvalidTransaction(
                f"account {account.id} balance would leave the supported range of "
                f"+/-{MAX_MAGNITUDE} minor units"
            )
        new_balances[account.id] = updated
    return new_balances


def transfer_entries(
    source: AccountState, destination: AccountState, amount: int, currency: str
) -> list[EntryDraft]:
    """Build the two legs that move ``amount`` from ``source`` to ``destination``.

    "From" and "to" only have an unambiguous meaning when both accounts grow on
    the same side - two customer wallets, or two bank accounts. Anything else
    (a deposit debits cash and credits a wallet) is an explicit journal
    transaction, so the caller states the debits and credits it means.
    """
    if amount <= 0:
        raise InvalidTransfer("amount must be positive")
    if source.id == destination.id:
        raise InvalidTransfer("source and destination accounts must be different")
    if source.normal_balance is not destination.normal_balance:
        raise InvalidTransfer(
            f"account {source.id} has a {source.normal_balance} normal balance but "
            f"{destination.id} has a {destination.normal_balance} normal balance; "
            "post an explicit transaction (POST /v1/transactions) instead"
        )
    # A debit-normal account grows with debits (+), a credit-normal one with credits (-).
    increase = amount if destination.normal_balance is NormalBalance.DEBIT else -amount
    return [
        EntryDraft(account_id=source.id, amount=-increase, currency=currency),
        EntryDraft(account_id=destination.id, amount=increase, currency=currency),
    ]


def reversal_entries(entries: Sequence[EntryDraft]) -> list[EntryDraft]:
    """The legs that cancel a posted transaction exactly: same accounts, opposite signs."""
    return [
        EntryDraft(account_id=entry.account_id, amount=-entry.amount, currency=entry.currency)
        for entry in entries
    ]


def hold_status(stored: HoldStatus, expires_at: datetime, now: datetime) -> HoldStatus:
    """The status a client sees: an active hold counts as expired from ``expires_at`` on."""
    if stored is HoldStatus.ACTIVE and now >= expires_at:
        return HoldStatus.EXPIRED
    return stored


def check_hold_request(
    source: AccountState,
    destination: AccountState,
    amount: int,
    currency: str,
    *,
    expires_at: datetime,
    now: datetime,
) -> None:
    """Check that a new hold may reserve ``amount`` on ``source`` for ``destination``.

    A hold is a transfer that has not happened yet, so it follows the transfer
    rules (two different accounts with the same normal balance side, in the
    accounts' currency) and the same funds rule: posting the transfer right now
    must be allowed, counting the source's other active holds.
    """
    if expires_at <= now:
        raise InvalidHold(
            f"expires_at ({timestamp(expires_at)}) must be later than the server's "
            f"current time ({timestamp(now)})",
            expires_at=timestamp(expires_at),
            server_time=timestamp(now),
        )
    drafts = transfer_entries(source, destination, amount, currency)
    accounts = {source.id: source, destination.id: destination}
    check_currencies(drafts, accounts)
    apply_entries(drafts, accounts)


def require_active(hold: HoldState, now: datetime) -> None:
    """Only an active hold can be captured or voided, and only before it expires."""
    status = hold_status(hold.status, hold.expires_at, now)
    if status is HoldStatus.EXPIRED:
        raise HoldExpired(hold.id, hold.expires_at)
    if status is not HoldStatus.ACTIVE:
        raise HoldNotActive(hold.id, status)


def capture_amount(hold: HoldState, requested: int | None) -> int:
    """The amount a capture posts: the requested amount, or the whole hold by default.

    A capture may take less than was authorized (the rest is released) but never more.
    """
    amount = hold.amount if requested is None else requested
    if amount <= 0:
        raise InvalidHold("the capture amount must be positive", hold_id=str(hold.id))
    if amount > hold.amount:
        raise InvalidHold(
            f"cannot capture {amount}: hold {hold.id} authorized only {hold.amount}",
            hold_id=str(hold.id),
            authorized_amount=hold.amount,
            requested_amount=amount,
        )
    return amount
