"""The ``ledgerline`` command-line entry point."""

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
