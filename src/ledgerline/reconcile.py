"""Reconciliation: recompute the ledger's invariants from the immutable entries.

The cached ``accounts.balance`` column exists for speed; entries are the
source of truth. ``ledgerline reconcile`` checks that the two agree and that
double-entry bookkeeping holds. Each check is one SQL statement, so each sees
a consistent snapshot even while the API keeps writing.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ledgerline.domain import balance_change
from ledgerline.models import Account, Entry, Transaction


@dataclass(frozen=True, slots=True)
class BalanceMismatch:
    account_id: UUID
    stored: int
    from_entries: int


@dataclass(frozen=True, slots=True)
class UnbalancedTransaction:
    transaction_id: UUID
    currency: str
    total: int


@dataclass(frozen=True, slots=True)
class CurrencyMismatch:
    entry_id: int
    account_id: UUID
    entry_currency: str
    account_currency: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    accounts: int
    transactions: int
    entries: int
    balance_mismatches: list[BalanceMismatch]
    unbalanced_transactions: list[UnbalancedTransaction]
    currency_mismatches: list[CurrencyMismatch]
    trial_balance: dict[str, int]
    """Sum of all entries per currency; double-entry bookkeeping makes each one zero."""

    @property
    def ok(self) -> bool:
        return not (
            self.balance_mismatches
            or self.unbalanced_transactions
            or self.currency_mismatches
            or any(self.trial_balance.values())
        )

    def render(self) -> str:
        lines = [
            f"accounts: {self.accounts}  transactions: {self.transactions}  entries: {self.entries}"
        ]

        def check(passed: bool, description: str, problems: list[str]) -> None:
            lines.append(f"[{'ok' if passed else 'FAIL'}] {description}")
            lines.extend(f"       {problem}" for problem in problems)

        check(
            not self.balance_mismatches,
            "stored balances equal the sum of their entries",
            [
                f"account {m.account_id}: stored {m.stored}, entries give {m.from_entries}"
                for m in self.balance_mismatches
            ],
        )
        check(
            not self.unbalanced_transactions,
            "every transaction sums to zero per currency",
            [
                f"transaction {u.transaction_id}: {u.currency} entries sum to {u.total}"
                for u in self.unbalanced_transactions
            ],
        )
        check(
            not self.currency_mismatches,
            "every entry uses its account's currency",
            [
                f"entry {c.entry_id}: {c.entry_currency} on account {c.account_id} "
                f"({c.account_currency})"
                for c in self.currency_mismatches
            ],
        )
        totals = ", ".join(f"{currency} {total}" for currency, total in self.trial_balance.items())
        check(
            not any(self.trial_balance.values()),
            f"trial balance is zero per currency ({totals or 'no entries yet'})",
            [],
        )
        lines.append("Ledger is consistent." if self.ok else "Ledger has discrepancies.")
        return "\n".join(lines)


def reconcile(session: Session) -> ReconciliationReport:
    return ReconciliationReport(
        accounts=_count(session, Account),
        transactions=_count(session, Transaction),
        entries=_count(session, Entry),
        balance_mismatches=_balance_mismatches(session),
        unbalanced_transactions=_unbalanced_transactions(session),
        currency_mismatches=_currency_mismatches(session),
        trial_balance=_trial_balance(session),
    )


def _count(session: Session, model: type[Account] | type[Transaction] | type[Entry]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _balance_mismatches(session: Session) -> list[BalanceMismatch]:
    rows = session.execute(
        select(
            Account.id,
            Account.normal_balance,
            Account.balance,
            func.coalesce(func.sum(Entry.amount), 0),
        )
        .outerjoin(Entry, Entry.account_id == Account.id)
        .group_by(Account.id, Account.normal_balance, Account.balance)
        .order_by(Account.id)
    )
    mismatches = []
    for account_id, normal_balance, stored, signed_total in rows:
        from_entries = balance_change(normal_balance, int(signed_total))
        if stored != from_entries:
            mismatches.append(BalanceMismatch(account_id, stored, from_entries))
    return mismatches


def _unbalanced_transactions(session: Session) -> list[UnbalancedTransaction]:
    total = func.sum(Entry.amount)
    rows = session.execute(
        select(Entry.transaction_id, Entry.currency, total)
        .group_by(Entry.transaction_id, Entry.currency)
        .having(total != 0)
        .order_by(Entry.transaction_id, Entry.currency)
    )
    return [
        UnbalancedTransaction(transaction_id, currency, int(amount))
        for transaction_id, currency, amount in rows
    ]


def _currency_mismatches(session: Session) -> list[CurrencyMismatch]:
    rows = session.execute(
        select(Entry.id, Entry.account_id, Entry.currency, Account.currency)
        .join(Account, Account.id == Entry.account_id)
        .where(Entry.currency != Account.currency)
        .order_by(Entry.id)
    )
    return [CurrencyMismatch(*row) for row in rows]


def _trial_balance(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Entry.currency, func.sum(Entry.amount))
        .group_by(Entry.currency)
        .order_by(Entry.currency)
    )
    return {currency: int(total) for currency, total in rows}
