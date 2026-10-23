"""Typed errors raised by the ledger.

Each error class carries the HTTP status, RFC 9457 problem type and title it
maps to, so the API layer can translate every failure the same way instead of
growing an if/elif chain.
"""

from typing import ClassVar
from uuid import UUID


class LedgerError(Exception):
    """A failure that should reach the client as an application/problem+json response."""

    status_code: ClassVar[int] = 400
    problem_type: ClassVar[str] = "bad-request"
    title: ClassVar[str] = "Bad request"

    def __init__(self, detail: str, **extensions: object) -> None:
        super().__init__(detail)
        self.detail = detail
        # Extra machine-readable members for the problem body (RFC 9457, section 3.2).
        self.extensions = extensions


class AuthenticationRequired(LedgerError):
    status_code = 401
    problem_type = "authentication-required"
    title = "Authentication required"


class InvalidIdempotencyKey(LedgerError):
    status_code = 400
    problem_type = "invalid-idempotency-key"
    title = "Missing or invalid Idempotency-Key header"


class InvalidCursor(LedgerError):
    status_code = 400
    problem_type = "invalid-cursor"
    title = "Invalid pagination cursor"


class NotFound(LedgerError):
    status_code = 404
    problem_type = "not-found"
    title = "Resource not found"


class AccountNotFound(NotFound):
    def __init__(self, account_id: UUID) -> None:
        super().__init__(f"account {account_id} does not exist", account_id=str(account_id))


class TransactionNotFound(NotFound):
    def __init__(self, transaction_id: UUID) -> None:
        super().__init__(
            f"transaction {transaction_id} does not exist", transaction_id=str(transaction_id)
        )


class InvalidTransaction(LedgerError):
    status_code = 422
    problem_type = "invalid-transaction"
    title = "Invalid transaction"


class UnbalancedTransaction(InvalidTransaction):
    problem_type = "unbalanced-transaction"
    title = "Entries do not balance"


class InvalidTransfer(LedgerError):
    status_code = 422
    problem_type = "invalid-transfer"
    title = "Invalid transfer"


class InsufficientFunds(LedgerError):
    status_code = 422
    problem_type = "insufficient-funds"
    title = "Insufficient funds"

    def __init__(self, account_id: UUID, *, balance: int, change: int) -> None:
        super().__init__(
            f"account {account_id} has a balance of {balance} and does not allow it to go "
            f"below zero; the requested change was {change}",
            account_id=str(account_id),
            balance=balance,
            change=change,
        )


class ReversalNotAllowed(LedgerError):
    status_code = 422
    problem_type = "reversal-not-allowed"
    title = "Transaction cannot be reversed"


class AlreadyReversed(LedgerError):
    status_code = 409
    problem_type = "already-reversed"
    title = "Transaction already reversed"

    def __init__(self, transaction_id: UUID, reversal_id: UUID) -> None:
        super().__init__(
            f"transaction {transaction_id} was already reversed by {reversal_id}",
            transaction_id=str(transaction_id),
            reversed_by_transaction_id=str(reversal_id),
        )


class IdempotencyKeyReused(LedgerError):
    status_code = 409
    problem_type = "idempotency-key-reused"
    title = "Idempotency key reused with a different request"


class ConcurrentUpdate(LedgerError):
    status_code = 409
    problem_type = "concurrent-update"
    title = "Concurrent update"


class DatabaseUnavailable(LedgerError):
    status_code = 503
    problem_type = "database-unavailable"
    title = "Database unavailable"
