"""Journal endpoints: post transactions and transfers, read and reverse them."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Request, Response, status

from ledgerline.api.dependencies import (
    IdempotencyKeyHeader,
    PrincipalDep,
    ServiceDep,
    require_idempotency,
    respond,
)
from ledgerline.schemas import ReversalCreate, TransactionCreate, TransactionOut, TransferCreate

router = APIRouter(tags=["transactions"])


@router.post(
    "/transactions", status_code=status.HTTP_201_CREATED, summary="Post a journal transaction"
)
def post_transaction(
    body: TransactionCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> TransactionOut:
    """Post two or more entries (debit > 0, credit < 0) that sum to zero per currency."""
    idempotency = require_idempotency(request, principal, idempotency_key, body)
    return respond(response, service.post_transaction(body, idempotency))


@router.post(
    "/transfers", status_code=status.HTTP_201_CREATED, summary="Move money between two accounts"
)
def create_transfer(
    body: TransferCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> TransactionOut:
    """Decrease `from` and increase `to` by `amount`; both must share a normal-balance side."""
    idempotency = require_idempotency(request, principal, idempotency_key, body)
    return respond(response, service.transfer(body, idempotency))


@router.get("/transactions/{transaction_id}", summary="Get a transaction")
def get_transaction(transaction_id: UUID, service: ServiceDep) -> TransactionOut:
    return service.get_transaction(transaction_id)


@router.post(
    "/transactions/{transaction_id}/reverse",
    status_code=status.HTTP_201_CREATED,
    summary="Reverse a transaction",
)
def reverse_transaction(
    transaction_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
    body: Annotated[ReversalCreate | None, Body()] = None,
) -> TransactionOut:
    """Post a new transaction with every entry negated. Posted transactions are never edited."""
    reversal = body or ReversalCreate()
    idempotency = require_idempotency(request, principal, idempotency_key, reversal)
    return respond(response, service.reverse_transaction(transaction_id, reversal, idempotency))
