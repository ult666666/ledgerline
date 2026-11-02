"""Reconciliation: recompute the ledger's invariants from the immutable entries.

The cached ``accounts.balance`` column exists for speed; entries are the
source of truth. ``ledgerline reconcile`` checks that the two agree and that
double-entry bookkeeping holds. It also checks the holds: their stored states
follow the hold state machine, active holds fit within the balance of
accounts that may not go negative, and each capture is in the journal exactly
once. Each check is one SQL statement, so each sees a consistent snapshot even
while the API keeps writing.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ledgerline.domain import STORED_HOLD_STATUSES, HoldStatus, balance_change
from ledgerline.models import Account, Entry, Hold, Transaction


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
class HoldStateViolation:
    """A hold whose stored fields describe no path through the hold state machine."""

    hold_id: UUID
    problem: str


@dataclass(frozen=True, slots=True)
class OvercommittedAccount:
    """An account that may not go negative, whose active holds exceed its balance."""

    account_id: UUID
    balance: int
    held: int


@dataclass(frozen=True, slots=True)
class CaptureMismatch:
    """A captured hold that is not in the journal exactly once, for the captured amount."""

    hold_id: UUID
    problem: str


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
    holds: int = 0
    hold_violations: list[HoldStateViolation] = field(default_factory=list)
    overcommitted_accounts: list[OvercommittedAccount] = field(default_factory=list)
    capture_mismatches: list[CaptureMismatch] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.balance_mismatches
            or self.unbalanced_transactions
            or self.currency_mismatches
            or any(self.trial_balance.values())
            or self.hold_violations
            or self.overcommitted_accounts
            or self.capture_mismatches
        )

    def render(self) -> str:
        lines = [
            f"accounts: {self.accounts}  transactions: {self.transactions}  "
            f"entries: {self.entries}  holds: {self.holds}"
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
        check(
            not self.hold_violations,
            "every hold's state, amounts and timestamps follow the hold state machine",
            [f"hold {v.hold_id}: {v.problem}" for v in self.hold_violations],
        )
        check(
            not self.overcommitted_accounts,
            "active holds fit within the balance of every account that cannot go negative",
            [
                f"account {o.account_id}: balance {o.balance}, active holds {o.held}"
                for o in self.overcommitted_accounts
            ],
        )
        check(
            not self.capture_mismatches,
            "every captured hold is in the journal exactly once, for the captured amount",
            [f"hold {m.hold_id}: {m.problem}" for m in self.capture_mismatches],
        )
        lines.append("Ledger is consistent." if self.ok else "Ledger has discrepancies.")
        return "\n".join(lines)


def reconcile(session: Session, *, now: datetime | None = None) -> ReconciliationReport:
    """Check every invariant. ``now`` decides which holds have expired (default: the clock)."""
    return ReconciliationReport(
        accounts=_count(session, Account),
        transactions=_count(session, Transaction),
        entries=_count(session, Entry),
        balance_mismatches=_balance_mismatches(session),
        unbalanced_transactions=_unbalanced_transactions(session),
        currency_mismatches=_currency_mismatches(session),
        trial_balance=_trial_balance(session),
        holds=_count(session, Hold),
        hold_violations=_hold_violations(session),
        overcommitted_accounts=_overcommitted_accounts(session, now or datetime.now(UTC)),
        capture_mismatches=_capture_mismatches(session),
    )


def _count(
    session: Session, model: type[Account] | type[Transaction] | type[Entry] | type[Hold]
) -> int:
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


def _hold_violations(session: Session) -> list[HoldStateViolation]:
    rows = session.execute(
        select(
            Hold.id,
            Hold.status,
            Hold.amount,
            Hold.captured_amount,
            Hold.created_at,
            Hold.expires_at,
            Hold.closed_at,
        ).order_by(Hold.id)
    )
    return [
        HoldStateViolation(hold_id, problem)
        for hold_id, status, amount, captured, created_at, expires_at, closed_at in rows
        for problem in hold_problems(status, amount, captured, created_at, expires_at, closed_at)
    ]


def hold_problems(
    status: str,
    amount: int,
    captured_amount: int | None,
    created_at: datetime,
    expires_at: datetime,
    closed_at: datetime | None,
) -> list[str]:
    """Why a stored hold could not have been reached through valid transitions.

    The valid paths are: created active (``created_at < expires_at``); then
    either nothing (active, or expired once ``expires_at`` passes), or exactly
    one capture or void at ``closed_at``, while it was still active
    (``created_at <= closed_at < expires_at``). A capture takes between 1 and
    the authorized amount; a void takes nothing.
    """
    if status not in STORED_HOLD_STATUSES:
        return [f"stored status {status!r} is not one of active, captured, voided"]
    problems = []
    if amount <= 0:
        problems.append(f"authorized amount {amount} is not positive")
    if expires_at <= created_at:
        problems.append("expires_at is not later than created_at")
    if status == HoldStatus.ACTIVE:
        if closed_at is not None or captured_amount is not None:
            problems.append("an active hold has a closing time or a captured amount")
        return problems
    if closed_at is None:
        problems.append(f"a {status} hold has no closed_at")
    elif closed_at < created_at:
        problems.append(f"{status} at {closed_at.isoformat()}, before it was created")
    elif closed_at >= expires_at:
        problems.append(f"{status} at {closed_at.isoformat()}, after it had expired")
    if status == HoldStatus.VOIDED and captured_amount is not None:
        problems.append("a voided hold has a captured amount")
    if status == HoldStatus.CAPTURED:
        if captured_amount is None:
            problems.append("a captured hold has no captured amount")
        elif not 0 < captured_amount <= amount:
            problems.append(f"captured {captured_amount} of an authorized {amount}")
    return problems


def _overcommitted_accounts(session: Session, now: datetime) -> list[OvercommittedAccount]:
    held = func.sum(Hold.amount)
    rows = session.execute(
        select(Account.id, Account.balance, held)
        .join(Hold, Hold.from_account_id == Account.id)
        .where(
            Account.allow_negative.is_(False),
            Hold.status == HoldStatus.ACTIVE,
            Hold.expires_at > now,
        )
        .group_by(Account.id, Account.balance)
        .having(held > Account.balance)
        .order_by(Account.id)
    )
    return [
        OvercommittedAccount(account_id, balance, int(total)) for account_id, balance, total in rows
    ]


def _capture_mismatches(session: Session) -> list[CaptureMismatch]:
    # Every hold that is captured or that some transaction claims to capture,
    # with that transaction's entries: one row per entry.
    rows = session.execute(
        select(
            Hold.id,
            Hold.status,
            Hold.from_account_id,
            Hold.to_account_id,
            Hold.currency,
            Hold.captured_amount,
            Hold.closed_at,
            Transaction.id,
            Transaction.posted_at,
            Transaction.reverses_id,
            Entry.account_id,
            Entry.amount,
            Entry.currency,
            Account.normal_balance,
        )
        .select_from(Hold)
        .outerjoin(Transaction, Transaction.hold_id == Hold.id)
        .outerjoin(Entry, Entry.transaction_id == Transaction.id)
        .outerjoin(Account, Account.id == Entry.account_id)
        .where(or_(Hold.status == HoldStatus.CAPTURED, Transaction.id.is_not(None)))
        .order_by(Hold.id, Transaction.id, Entry.id)
    )
    holds: dict[UUID, _CaptureEvidence] = {}
    for row in rows:
        hold_id, status, source, destination, currency, captured, closed_at = row[:7]
        evidence = holds.setdefault(
            hold_id, _CaptureEvidence(status, source, destination, currency, captured, closed_at)
        )
        transaction_id, posted_at, reverses_id, account_id, amount, entry_currency, side = row[7:]
        if transaction_id is not None:
            evidence.transactions[transaction_id] = (posted_at, reverses_id)
        if account_id is not None:
            evidence.legs[transaction_id][account_id] = balance_change(side, amount)
            evidence.currencies.add(entry_currency)
    return [
        CaptureMismatch(hold_id, problem)
        for hold_id, evidence in holds.items()
        for problem in evidence.problems()
    ]


@dataclass
class _CaptureEvidence:
    status: str
    source: UUID
    destination: UUID
    currency: str
    captured_amount: int | None
    closed_at: datetime | None
    transactions: dict[UUID, tuple[datetime, UUID | None]] = field(default_factory=dict)
    legs: defaultdict[UUID, dict[UUID, int]] = field(default_factory=lambda: defaultdict(dict))
    currencies: set[str] = field(default_factory=set)

    def problems(self) -> list[str]:
        if self.status != HoldStatus.CAPTURED:
            return [f"the hold is {self.status}, yet a journal transaction captures it"]
        if not self.transactions:
            return ["captured, but no journal transaction records the capture"]
        if len(self.transactions) > 1:
            return [f"captured once but posted {len(self.transactions)} times"]
        [(transaction_id, (posted_at, reverses_id))] = self.transactions.items()
        problems = []
        # In each account's own terms, the source loses and the destination
        # gains exactly the captured amount, and nothing else moves.
        expected = {
            self.source: -(self.captured_amount or 0),
            self.destination: self.captured_amount,
        }
        if self.legs[transaction_id] != expected:
            problems.append(
                f"transaction {transaction_id} moves {_describe(self.legs[transaction_id])}, "
                f"not {self.captured_amount} from {self.source} to {self.destination}"
            )
        if self.currencies != {self.currency}:
            problems.append(f"transaction {transaction_id} is not entirely in {self.currency}")
        if posted_at != self.closed_at:
            problems.append(f"transaction {transaction_id} was not posted when the hold closed")
        if reverses_id is not None:
            problems.append(f"transaction {transaction_id} is a reversal")
        return problems


def _describe(legs: dict[UUID, int]) -> str:
    return (
        ", ".join(f"{change:+d} on {account_id}" for account_id, change in legs.items())
        or "nothing"
    )
