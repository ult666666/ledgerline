"""Structured JSON logging with a per-request ID.

Every log line is one JSON object, so a log pipeline can filter on fields
(``request_id``, ``status``, ``duration_ms``) instead of parsing free text.
The request ID lives in a context variable, which follows the request into
the worker thread that runs the endpoint, so log calls anywhere in the code
are tagged without passing the ID around.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_HANDLER_NAME = "ledgerline-json"


class JsonFormatter(logging.Formatter):
    """Render a log record as a single line of JSON.

    Structured fields are passed explicitly: ``logger.info("msg", extra={"fields": {...}})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Send all logs (ours, uvicorn's, SQLAlchemy's) to stdout as JSON lines.

    Safe to call more than once: it replaces only the handler it installed.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Let uvicorn's loggers propagate to the root handler instead of using
    # their own plain-text handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
