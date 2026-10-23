"""Settings parsing."""

import pytest
from pydantic import ValidationError

from ledgerline.config import DEFAULT_DATABASE_URL, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "LEDGERLINE_API_KEYS", "LEDGERLINE_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.api_keys == []
    assert settings.log_level == "INFO"


def test_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./other.db")
    monkeypatch.setenv("LEDGERLINE_API_KEYS", " first-key , second-key,, ")
    monkeypatch.setenv("LEDGERLINE_LOG_LEVEL", "debug")
    settings = Settings(_env_file=None)
    assert settings.database_url == "sqlite:///./other.db"
    assert settings.api_keys == ["first-key", "second-key"]
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@db:5432/ledger", "postgresql+psycopg://u:p@db:5432/ledger"),
        ("postgresql://u:p@db:5432/ledger", "postgresql+psycopg://u:p@db:5432/ledger"),
        ("postgresql+psycopg://u:p@db/ledger", "postgresql+psycopg://u:p@db/ledger"),
        ("sqlite:///./ledgerline.db", "sqlite:///./ledgerline.db"),
    ],
)
def test_postgres_urls_use_the_psycopg_driver(given: str, expected: str) -> None:
    assert Settings(database_url=given, _env_file=None).database_url == expected


def test_rejects_unknown_log_levels() -> None:
    with pytest.raises(ValidationError, match="unknown log level"):
        Settings(log_level="chatty", _env_file=None)


def test_api_keys_are_kept_out_of_repr() -> None:
    settings = Settings(api_keys=["super-secret-value"], _env_file=None)
    assert "super-secret-value" not in repr(settings)
