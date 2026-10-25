"""Run a real HTTP wallet lifecycle in a disposable, owned SQLite service.

Usage: ``uv run python examples/wallet_demo.py``. No existing database, server,
API key, Docker or payment provider is used. See docs/WALLET_DEMO.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.client
import json
import math
import multiprocessing
import secrets
import socket
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.db import create_db_engine
from ledgerline.reconcile import reconcile

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]


class DemoError(RuntimeError):
    """A deadline, HTTP response or accounting invariant prevented completion."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: Json
    headers: dict[str, str]

    def expect(self, status: int) -> Json:
        if self.status != status:
            raise DemoError(f"expected HTTP {status}, got {self.status}: {self.body}")
        return self.body


def serve(database_url: str, api_key: str, listener: socket.socket) -> None:
    """Migrate and serve only the explicitly supplied disposable database/socket."""
    try:
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        app = create_app(
            Settings(
                database_url=database_url,
                api_keys=[api_key],
                log_level="CRITICAL",
                _env_file=None,
            )
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=None,
                access_log=False,
                log_level="critical",
                timeout_graceful_shutdown=1,
            )
        )
        server.run(sockets=[listener])
    finally:
        listener.close()


class DemoServer:
    """Own a private directory, bound loopback socket and spawned API process."""

    def __init__(self, timeout: float = 15.0) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number of seconds")
        self.timeout = timeout
        self.port = 0
        self.server_pid: int | None = None
        self.server_stopped = False
        self.database_path: Path | None = None
        self.database_url = ""
        self.process: BaseProcess | None = None
        self._directory: TemporaryDirectory[str] | None = None
        self._listener: socket.socket | None = None
        self._deadline = 0.0
        self._api_key = secrets.token_urlsafe(24)

    def __enter__(self) -> DemoServer:
        self._deadline = time.monotonic() + self.timeout
        try:
            self._directory = TemporaryDirectory(prefix="ledgerline-wallet-demo-")
            self.database_path = Path(self._directory.name) / "wallet.sqlite"
            self.database_url = URL.create(
                "sqlite", database=str(self.database_path)
            ).render_as_string(hide_password=False)
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(128)
            self.port = self._listener.getsockname()[1]
            self.process = multiprocessing.get_context("spawn").Process(
                target=serve,
                args=(self.database_url, self._api_key, self._listener),
                daemon=True,
            )
            self.process.start()
            self.server_pid = self.process.pid
            # spawn duplicated the bound socket for the child. No port is ever
            # released and reacquired, so readiness cannot select another server.
            self._listener.close()
            self._listener = None
            while time.monotonic() < self._deadline:
                if not self.process.is_alive():
                    raise DemoError(f"owned server exited during startup: {self.process.exitcode}")
                try:
                    if self.request("GET", "/readyz").status == 200:
                        return self
                except DemoError:
                    pass
                time.sleep(0.02)
            raise DemoError("startup deadline exceeded")
        except BaseException:
            self.close()
            raise

    def request(
        self, method: str, path: str, body: Json | None = None, key: str | None = None
    ) -> Reply:
        """Send HTTP only to this object's owned loopback listener, within its deadline."""
        if self.process is None:
            raise DemoError("owned server is not running")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise DemoError("workflow deadline exceeded")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=min(remaining, 1.0))
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            connection.request(
                method, path, body=None if body is None else json.dumps(body), headers=headers
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise DemoError("expected a JSON object from the owned server")
            return Reply(response.status, payload, {k.lower(): v for k, v in response.getheaders()})
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise DemoError(f"owned HTTP request failed: {exc}") from exc
        finally:
            connection.close()

    def close(self) -> None:
        """Stop only the owned process, then release its listener and directory."""
        try:
            if self.process is not None:
                if self.process.pid is not None:
                    if self.process.is_alive():
                        self.process.terminate()
                    self.process.join(timeout=2)
                    if self.process.is_alive():
                        self.process.kill()
                        self.process.join(timeout=2)
                    if self.process.is_alive():
                        raise DemoError("could not stop the owned server after terminate/kill")
                self.server_stopped = True
                self.process.close()
                self.process = None
        finally:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            if self._directory is not None:
                self._directory.cleanup()
                self._directory = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def exercise_wallet(server: DemoServer) -> Json:
    """Post, replay, reject and reverse through real HTTP; check each invariant."""
    accounts = {}
    for name, account_type in (("cash", "asset"), ("alice", "liability"), ("bob", "liability")):
        account = server.request(
            "POST", "/v1/accounts", {"name": name, "type": account_type, "currency": "USD"}
        ).expect(201)
        accounts[name] = str(account["id"])

    def balances(as_of: str | None = None) -> dict[str, int]:
        query = "" if as_of is None else "?" + urlencode({"as_of": as_of})
        return {
            name: int(
                server.request("GET", f"/v1/accounts/{account_id}/balance{query}").expect(200)[
                    "balance"
                ]
            )
            for name, account_id in accounts.items()
        }

    deposit = server.request(
        "POST",
        "/v1/transactions",
        {
            "description": "Fictional wallet top-up",
            "entries": [
                {"account_id": accounts["cash"], "amount": 10000, "currency": "USD"},
                {"account_id": accounts["alice"], "amount": -10000, "currency": "USD"},
            ],
        },
        key="deposit-alice",
    ).expect(201)
    transfer_body = {
        "from_account_id": accounts["alice"],
        "to_account_id": accounts["bob"],
        "amount": 2500,
        "currency": "USD",
        "description": "Fictional dinner payment",
    }
    transfer = server.request("POST", "/v1/transfers", transfer_body, key="dinner").expect(201)
    after_transfer = balances()
    replay_response = server.request("POST", "/v1/transfers", transfer_body, key="dinner")
    replay = replay_response.expect(201)
    after_replay = balances()
    overdraft = server.request(
        "POST",
        "/v1/transfers",
        {
            "from_account_id": accounts["bob"],
            "to_account_id": accounts["alice"],
            "amount": 100000,
            "currency": "USD",
        },
        key="overdraft",
    )
    rejection = overdraft.expect(422)
    after_rejection = balances()
    reversal = server.request(
        "POST",
        f"/v1/transactions/{transfer['id']}/reverse",
        {"description": "Fictional refund"},
        key="refund",
    ).expect(201)
    current = balances()
    historical = balances(str(transfer["posted_at"]))
    reread = server.request("GET", f"/v1/transactions/{transfer['id']}").expect(200)
    expected_transfer = {"cash": 10000, "alice": 7500, "bob": 2500}
    checks = {
        "transfer_balances": after_transfer == expected_transfer,
        "replay_same_id": replay["id"] == transfer["id"],
        "replay_header": replay_response.headers.get("idempotent-replayed") == "true",
        "replay_unchanged": after_replay == after_transfer,
        "overdraft_problem": str(rejection["type"]).endswith("#insufficient-funds"),
        "overdraft_unchanged": after_rejection == after_replay,
        "reversal_links": reversal["reverses_transaction_id"] == transfer["id"]
        and reread["reversed_by_transaction_id"] == reversal["id"],
        "original_entries_unchanged": reread["entries"] == transfer["entries"],
        "current_balances": current == {"cash": 10000, "alice": 10000, "bob": 0},
        "historical_balances": historical == expected_transfer,
    }
    if not all(checks.values()):
        raise DemoError(f"wallet invariant failed: {checks}")
    return {
        "scenario": "Disposable SQLite wallet lifecycle over loopback HTTP",
        "currency": "USD",
        "amount_unit": "integer cents",
        "account_ids": accounts,
        "transaction_ids": {
            "deposit": deposit["id"],
            "transfer": transfer["id"],
            "replay": replay["id"],
            "reversal": reversal["id"],
        },
        "after_transfer": after_transfer,
        "after_replay": after_replay,
        "overdraft": {"status": overdraft.status, "before": after_replay, "after": after_rejection},
        "historical": {"as_of": transfer["posted_at"], "balances": historical},
        "current": current,
        "invariants": checks,
    }


def run_demo(timeout: float = 15.0) -> Json:
    """Return a checked receipt only after shutting down and deleting owned resources."""
    server = DemoServer(timeout)
    with server:
        receipt = exercise_wallet(server)
        engine = create_db_engine(server.database_url)
        try:
            with Session(engine) as session:
                report = reconcile(session)
        finally:
            engine.dispose()
        if not report.ok or (report.accounts, report.transactions, report.entries) != (3, 3, 6):
            raise DemoError(report.render())
        receipt["reconciliation"] = {"ok": report.ok, **dataclasses.asdict(report)}
    receipt["cleanup"] = {
        "host": "127.0.0.1",
        "port": server.port,
        "server_pid": server.server_pid,
        "server_stopped": server.server_stopped,
        "database_path": str(server.database_path),
        "database_removed": server.database_path is not None and not server.database_path.exists(),
    }
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="shared startup/HTTP deadline in seconds"
    )
    args = parser.parse_args(argv)
    try:
        receipt = run_demo(args.timeout)
    except (DemoError, ValueError, OSError) as exc:
        print(f"wallet-demo: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
