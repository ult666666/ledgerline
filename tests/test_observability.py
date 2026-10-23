"""Structured logging and the access-log middleware."""

import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from ledgerline.api.auth import key_fingerprint
from ledgerline.observability import JsonFormatter, configure_logging, request_id_var
from tests.support import API_KEY


def make_record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("ledgerline.test", logging.INFO, __file__, 1, message, None, None)
    for name, value in extra.items():
        setattr(record, name, value)
    return record


def test_json_formatter_emits_one_object_per_line() -> None:
    token = request_id_var.set("req-1")
    try:
        line = JsonFormatter().format(make_record("hello", fields={"status": 201}))
    finally:
        request_id_var.reset(token)
    payload = json.loads(line)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "ledgerline.test"
    assert payload["request_id"] == "req-1"
    assert payload["status"] == 201
    assert payload["time"].endswith("+00:00")
    assert "\n" not in line


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("bad")
    except ValueError:
        record = make_record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: bad" in payload["exception"]
    assert "request_id" not in payload


def test_configure_logging_is_idempotent() -> None:
    configure_logging("DEBUG")
    configure_logging("INFO")
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "ledgerline-json"]
    assert len(ours) == 1
    assert isinstance(ours[0].formatter, JsonFormatter)
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("uvicorn.error").propagate


def test_every_request_is_logged_with_timing_and_client(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="ledgerline.http"):
        client.get("/v1/accounts", headers={"X-Request-ID": "req-log-test"})

    [record] = [r for r in caplog.records if r.name == "ledgerline.http"]
    fields = record.__dict__["fields"]
    assert fields["method"] == "GET"
    assert fields["path"] == "/v1/accounts"
    assert fields["status"] == 200
    assert fields["duration_ms"] >= 0
    # The client is identified by a fingerprint of its key, never the key itself.
    assert fields["client"] == key_fingerprint(API_KEY)
    assert API_KEY not in json.dumps(fields)
