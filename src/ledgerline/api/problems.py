"""RFC 9457 problem details (application/problem+json) for every error response."""

from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledgerline.errors import AuthenticationRequired, ConcurrentUpdate, LedgerError
from ledgerline.observability import request_id_var

PROBLEM_CONTENT_TYPE = "application/problem+json"
# Each problem type URI points at its entry in the error catalogue.
PROBLEM_TYPE_BASE = "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#"


def problem_response(
    status: int,
    title: str,
    *,
    detail: str | None = None,
    problem_type: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: Mapping[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": PROBLEM_TYPE_BASE + problem_type if problem_type else "about:blank",
        "title": title,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail
    body.update(extensions or {})
    request_id = request_id_var.get()
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(
        body, status_code=status, headers=dict(headers or {}), media_type=PROBLEM_CONTENT_TYPE
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerError)
    async def _ledger_error(_request: Request, exc: LedgerError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, AuthenticationRequired):
            headers["WWW-Authenticate"] = "Bearer"
        elif isinstance(exc, ConcurrentUpdate):
            headers["Retry-After"] = "1"
        return problem_response(
            exc.status_code,
            exc.title,
            detail=exc.detail,
            problem_type=exc.problem_type,
            headers=headers,
            extensions=exc.extensions,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location, message and type: echoing the rejected input back could
        # leak data into logs and client error reports.
        errors = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return problem_response(
            422,
            "Request validation failed",
            detail="The request body, path or query parameters are invalid.",
            problem_type="validation-error",
            extensions={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = HTTPStatus(exc.status_code).phrase
        detail = None if exc.detail == title else str(exc.detail)
        return problem_response(exc.status_code, title, detail=detail, headers=exc.headers)
