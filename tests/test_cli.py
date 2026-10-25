"""The ``ledgerline`` command-line entry point."""

import secrets
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from ledgerline import cli


def test_serve_runs_uvicorn_with_json_logging(
    monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert cli.main(["serve", "--host", "192.0.2.10", "--port", "9000"]) == 0

    [(app, kwargs)] = calls
    assert isinstance(app, FastAPI)
    assert kwargs == {"host": "192.0.2.10", "port": 9000, "log_config": None, "access_log": False}


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2
    assert "serve" in capsys.readouterr().err


def test_generate_key_does_not_load_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_random_token(nbytes: int) -> str:
        assert nbytes == 32
        return "synthetic-test-token"

    def unexpected_settings() -> None:
        raise AssertionError("key generation must not read application settings")

    monkeypatch.setattr(secrets, "token_urlsafe", fake_random_token)
    monkeypatch.setattr(cli, "Settings", unexpected_settings)
    assert cli.main(["generate-key"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "synthetic-test-token\n"
    assert captured.err == ""


def test_generate_key_output_is_urlsafe_and_correct_length(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import re

    assert cli.main(["generate-key"]) == 0
    token = capsys.readouterr().out.strip()
    # Base64url without padding encodes 32 random bytes in 43 characters.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
