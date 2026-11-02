"""Hold endpoints: authorize, capture, void and read holds (two-phase transfers)."""

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
from ledgerline.schemas import HoldCapture, HoldCreate, HoldOut

router = APIRouter(prefix="/holds", tags=["holds"])


@router.post("", status_code=status.HTTP_201_CREATED, summary="Authorize a hold")
def create_hold(
    body: HoldCreate,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> HoldOut:
    """Reserve `amount` on `from` for a later transfer to `to`. Nothing is posted yet.

    The hold follows the transfer rules and needs the amount to be available on
    `from`. Until it is captured, voided or expires it lowers that account's
    available balance.
    """
    idempotency = require_idempotency(request, principal, idempotency_key, body)
    return respond(response, service.create_hold(body, idempotency))


@router.get("/{hold_id}", summary="Get a hold")
def get_hold(hold_id: UUID, service: ServiceDep) -> HoldOut:
    return service.get_hold(hold_id)


@router.post("/{hold_id}/capture", summary="Capture a hold, fully or partly")
def capture_hold(
    hold_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
    body: Annotated[HoldCapture | None, Body()] = None,
) -> HoldOut:
    """Post the transfer for `amount` (default: all of it) and release the rest of the hold."""
    capture = body or HoldCapture()
    idempotency = require_idempotency(request, principal, idempotency_key, capture)
    return respond(response, service.capture_hold(hold_id, capture, idempotency))


@router.post("/{hold_id}/void", summary="Void a hold")
def void_hold(
    hold_id: UUID,
    request: Request,
    response: Response,
    principal: PrincipalDep,
    service: ServiceDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> HoldOut:
    """Release the whole hold without posting anything."""
    idempotency = require_idempotency(request, principal, idempotency_key, None)
    return respond(response, service.void_hold(hold_id, idempotency))
