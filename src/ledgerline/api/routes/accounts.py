"""Account endpoints: open accounts, list them, read balances and statements."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import AwareDatetime

from ledgerline.api.dependencies import (
    IdempotencyKeyHeader,
    PrincipalDep,
    ServiceDep,
    optional_idempotency,
    respond,
)
from ledgerline.schemas import AccountCreate, AccountOut, BalanceOut, Page, StatementLine

router = APIRouter(prefix="/accounts", tags=["accounts"])

PageLimit = Annotated[int, Query(ge=1, le=100, description="Page size.")]
PageCursor = Annotated[str | None, Query(description="next_cursor from the previous page.")]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Open an account")
def create_account(
    body: AccountCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> AccountOut:
    """Opening an account moves no money, so Idempotency-Key is optional here."""
    idempotency = optional_idempotency(request, principal, idempotency_key, body)
    return respond(response, service.create_account(body, idempotency))


@router.get("", summary="List accounts, oldest first")
def list_accounts(
    service: ServiceDep, limit: PageLimit = 50, cursor: PageCursor = None
) -> Page[AccountOut]:
    return service.list_accounts(limit=limit, cursor=cursor)


@router.get("/{account_id}", summary="Get an account")
def get_account(account_id: UUID, service: ServiceDep) -> AccountOut:
    return service.get_account(account_id)


@router.get("/{account_id}/balance", summary="Get the current or a point-in-time balance")
def get_balance(
    account_id: UUID,
    service: ServiceDep,
    as_of: Annotated[
        AwareDatetime | None,
        Query(description="ISO 8601 timestamp with a timezone, e.g. 2026-01-31T23:59:59Z."),
    ] = None,
) -> BalanceOut:
    return service.get_balance(account_id, as_of=as_of)


@router.get("/{account_id}/entries", summary="Account statement in posting order")
def list_entries(
    account_id: UUID, service: ServiceDep, limit: PageLimit = 50, cursor: PageCursor = None
) -> Page[StatementLine]:
    return service.list_entries(account_id, limit=limit, cursor=cursor)
