"""Double-entry rules as pure functions: no database, no HTTP.

Sign convention: an entry's ``amount`` is in integer minor units (cents for
USD) and is *signed* - positive for a debit, negative for a credit. A journal
transaction is balanced when, for every currency, its amounts sum to zero
(total debits == total credits).

Balances are reported in each account's own terms ("normal balance"): an asset
account holding $100 has balance 10000, and so does a customer wallet (a
liability) that owes the customer $100, even though the first was built from
debits and the second from credits.
"""

import enum
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ledgerline.errors import (
    InsufficientFunds,
    InvalidTransaction,
    InvalidTransfer,
    UnbalancedTransaction,
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


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the posting rules need to know about an account, detached from the ORM."""

    id: UUID
    currency: str
    normal_balance: NormalBalance
    allow_negative: bool
    balance: int
    version: int


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
    would drop below zero.
    """
    new_balances: dict[UUID, int] = {}
    for entry in entries:
        account = accounts[entry.account_id]
        current = new_balances.get(account.id, account.balance)
        change = balance_change(account.normal_balance, entry.amount)
        updated = current + change
        if updated < 0 and not account.allow_negative:
            raise InsufficientFunds(account.id, balance=current, change=change)
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
