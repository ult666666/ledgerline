"""Alembic migrations build exactly the schema the ORM models describe."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.db import create_db_engine
from ledgerline.models import Base
from tests.support import API_KEY, deposit, open_account

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def migrate(url: str, revision: str = "head") -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision)
    return config


def test_upgrade_head_matches_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    migrate(url)
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    assert differences == []


def test_the_api_runs_on_a_migrated_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    migrate(url)
    app = create_app(Settings(database_url=url, api_keys=[API_KEY], _env_file=None))
    with TestClient(app, headers={"Authorization": f"Bearer {API_KEY}"}) as client:
        cash = open_account(client, name="Cash")
        wallet = open_account(client, name="Wallet", account_type="liability")
        deposit(client, cash, wallet, 100)
        assert client.get(f"/v1/accounts/{wallet['id']}/balance").json()["balance"] == 100


def test_downgrade_removes_every_table(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.downgrade(migrate(url), "base")
    engine = create_db_engine(url)
    try:
        assert inspect(engine).get_table_names() == ["alembic_version"]
    finally:
        engine.dispose()
