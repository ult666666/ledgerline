"""Runtime configuration, read from environment variables (and an optional .env file)."""

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite:///./ledgerline.db"


class Settings(BaseSettings):
    """Service settings.

    ``DATABASE_URL`` keeps its conventional unprefixed name so it works with the
    usual hosting conventions; everything ledgerline-specific uses the
    ``LEDGERLINE_`` prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="LEDGERLINE_", env_file=".env", extra="ignore", validate_by_name=True
    )

    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        validation_alias="DATABASE_URL",
    )
    # Bearer tokens accepted by the API, given as a comma-separated list.
    # repr=False keeps them out of logs and tracebacks that print the settings.
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list, repr=False)
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        # Hosting providers hand out postgres:// URLs; SQLAlchemy would pick the
        # psycopg2 driver for those, but ledgerline ships with psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value.removeprefix(prefix)
        return value

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [key.strip() for key in value.split(",") if key.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unknown log level {value!r}")
        return level
