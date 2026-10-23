"""Request-ID, timing and access-log middleware."""

import logging
import re
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ledgerline.api.problems import problem_response
from ledgerline.observability import request_id_var

logger = logging.getLogger("ledgerline.http")

REQUEST_ID_HEADER = "X-Request-ID"
# Reuse a caller's request ID (so logs join up across services) only when it
# is short and plain; anything else could be used to pollute the logs.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class RequestContextMiddleware:
    """Give every request an ID, time it, log one line for it, and turn crashes into 500s.

    This is plain ASGI middleware rather than Starlette's BaseHTTPMiddleware so
    the endpoint runs in the same context and sees the request-ID context
    variable, and so a crash still produces a problem response carrying the ID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception("unhandled error")
            if response_started:
                raise
            response = problem_response(
                500,
                "Internal Server Error",
                detail="An unexpected error occurred. Quote the request_id when reporting it.",
            )
            await response(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request completed",
                extra={
                    "fields": {
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        # Set by the authentication dependency: a key fingerprint, never the key.
                        "client": scope.get("state", {}).get("principal"),
                    }
                },
            )
            request_id_var.reset(token)


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None
