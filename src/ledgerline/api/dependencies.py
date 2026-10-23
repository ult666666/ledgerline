"""FastAPI dependencies and small helpers shared by the routes."""

from typing import Annotated

from fastapi import Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ledgerline.api.auth import authenticate_api_key
from ledgerline.config import Settings
from ledgerline.errors import AuthenticationRequired, InvalidIdempotencyKey
from ledgerline.idempotency import IdempotencyRequest, check_key_format, request_fingerprint
from ledgerline.service import LedgerService, WriteResult

_bearer = HTTPBearer(auto_error=False, description="One of the keys in LEDGERLINE_API_KEYS.")


def get_service(request: Request) -> LedgerService:
    service: LedgerService = request.app.state.service
    return service


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Resolve the bearer token to a principal (the key's fingerprint) or reject the request."""
    if credentials is None:
        raise AuthenticationRequired("send an API key as 'Authorization: Bearer <key>'")
    settings: Settings = request.app.state.settings
    principal = authenticate_api_key(credentials.credentials, settings.api_keys)
    if principal is None:
        raise AuthenticationRequired("the API key is not valid")
    # Read by the access-log middleware.
    request.state.principal = principal
    return principal


ServiceDep = Annotated[LedgerService, Depends(get_service)]
PrincipalDep = Annotated[str, Depends(authenticate)]
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Unique per logical request (a UUID works well). Retrying with the same "
        "key and body replays the original response instead of repeating the write.",
    ),
]


def optional_idempotency(
    request: Request, principal: str, key: str | None, body: BaseModel | None
) -> IdempotencyRequest | None:
    if key is None:
        return None
    check_key_format(key)
    return IdempotencyRequest(
        principal=principal,
        key=key,
        fingerprint=request_fingerprint(request.method, request.url.path, body),
    )


def require_idempotency(
    request: Request, principal: str, key: str | None, body: BaseModel | None
) -> IdempotencyRequest:
    idempotency = optional_idempotency(request, principal, key, body)
    if idempotency is None:
        raise InvalidIdempotencyKey(
            "this endpoint moves money and requires an Idempotency-Key header"
        )
    return idempotency


def respond[BodyT: BaseModel](response: Response, result: WriteResult[BodyT]) -> BodyT:
    """Return the body of a write, flagging responses replayed from an idempotency key."""
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return result.body
