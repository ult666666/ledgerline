"""Exercise a running ledgerline API end to end: the Docker Compose check in CI.

Usage: ``python scripts/compose_smoke.py [--base-url URL]`` with the API key in
``LEDGERLINE_API_KEY``. It needs only the standard library, so it runs outside
the project environment against any deployment.

It opens three accounts, then checks a deposit, a transfer, a replayed retry, a
rejected key reuse and a refused overdraft. Last, it sends concurrent transfers
that together ask for more than one wallet holds, and checks that exactly the
affordable ones succeed and the balances still add up.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

Json = dict[str, Any]


class SmokeError(RuntimeError):
    """The API answered differently from its documented behaviour."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: Json
    headers: dict[str, str]

    def expect(self, status: int, what: str) -> Json:
        if self.status != status:
            raise SmokeError(f"{what}: expected HTTP {status}, got {self.status}: {self.body}")
        return self.body


class Client:
    def __init__(self, base_url: str, api_key: str) -> None:
        url = urlsplit(base_url)
        if url.scheme != "http" or not url.hostname:
            raise ValueError(f"expected an http:// base URL, got {base_url!r}")
        self._host = url.hostname
        self._port = url.port or 80
        self._api_key = api_key

    def call(
        self, method: str, path: str, body: Json | None = None, key: str | None = None
    ) -> Reply:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        connection = http.client.HTTPConnection(self._host, self._port, timeout=10)
        try:
            payload = None if body is None else json.dumps(body)
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return Reply(
                response.status,
                json.loads(raw) if raw else {},
                {name.lower(): value for name, value in response.getheaders()},
            )
        finally:
            connection.close()


def wait_until_ready(client: Client, deadline: float) -> None:
    while True:
        try:
            if client.call("GET", "/readyz").status == 200:
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SmokeError("the API did not report ready in time")
        time.sleep(1)


def open_account(client: Client, name: str, kind: str) -> str:
    body = {"name": name, "type": kind, "currency": "USD"}
    account_id: str = client.call("POST", "/v1/accounts", body).expect(201, f"open {name}")["id"]
    return account_id


def balance(client: Client, account_id: str) -> int:
    value: int = client.call("GET", f"/v1/accounts/{account_id}").expect(200, "read")["balance"]
    return value


def transfer_with_retries(client: Client, body: Json, key: str) -> Reply:
    """Retry a concurrency conflict with the same key, as the API asks clients to."""
    for _ in range(30):
        reply = client.call("POST", "/v1/transfers", body, key=key)
        if reply.status != 409 or not str(reply.body.get("type", "")).endswith("concurrent-update"):
            return reply
        time.sleep(0.05)
    raise SmokeError(f"transfer {key} still conflicted after 30 attempts")


def run(client: Client, run_id: str) -> Json:
    cash = open_account(client, "Cash at bank", "asset")
    alice = open_account(client, "Alice wallet", "liability")
    bob = open_account(client, "Bob wallet", "liability")

    deposit = {
        "description": "Card top-up",
        "entries": [
            {"account_id": cash, "amount": 10000, "currency": "USD"},
            {"account_id": alice, "amount": -10000, "currency": "USD"},
        ],
    }
    client.call("POST", "/v1/transactions", deposit, key=f"{run_id}-deposit").expect(201, "deposit")

    dinner = {"from_account_id": alice, "to_account_id": bob, "amount": 2500, "currency": "USD"}
    first = client.call("POST", "/v1/transfers", dinner, key=f"{run_id}-dinner").expect(
        201, "transfer"
    )
    retry = client.call("POST", "/v1/transfers", dinner, key=f"{run_id}-dinner")
    if (
        retry.expect(201, "retry")["id"] != first["id"]
        or retry.headers.get("idempotent-replayed") != "true"
    ):
        raise SmokeError(f"the retry was not replayed: {retry}")
    reused = client.call(
        "POST", "/v1/transfers", {**dinner, "amount": 9900}, key=f"{run_id}-dinner"
    )
    reused.expect(409, "key reuse with a different request")
    rent = {"from_account_id": bob, "to_account_id": alice, "amount": 100000, "currency": "USD"}
    client.call("POST", "/v1/transfers", rent, key=f"{run_id}-rent").expect(422, "overdraft")

    # Alice holds 7,500 cents. Twenty concurrent 500-cent transfers ask for
    # 10,000: exactly fifteen fit, and her wallet must never go below zero.
    payment = {"from_account_id": alice, "to_account_id": bob, "amount": 500, "currency": "USD"}
    with ThreadPoolExecutor(max_workers=20) as pool:
        replies = list(
            pool.map(
                lambda i: transfer_with_retries(client, payment, f"{run_id}-burst-{i}"), range(20)
            )
        )
    statuses = sorted(reply.status for reply in replies)
    if statuses != [201] * 15 + [422] * 5:
        raise SmokeError(f"concurrent transfers: expected 15 x 201 and 5 x 422, got {statuses}")

    balances = {
        "cash": balance(client, cash),
        "alice": balance(client, alice),
        "bob": balance(client, bob),
    }
    if balances != {"cash": 10000, "alice": 0, "bob": 10000}:
        raise SmokeError(f"unexpected balances after the run: {balances}")
    return {"concurrent_transfers": {"posted": 15, "refused": 5}, "balances_cents": balances}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for /readyz")
    args = parser.parse_args(argv)
    api_key = os.environ.get("LEDGERLINE_API_KEY", "")
    if not api_key:
        print("compose-smoke: set LEDGERLINE_API_KEY", file=sys.stderr)
        return 2
    client = Client(args.base_url, api_key)
    try:
        wait_until_ready(client, time.monotonic() + args.timeout)
        summary = run(client, run_id=f"smoke-{time.time_ns()}")
    except (SmokeError, OSError, ValueError) as exc:
        print(f"compose-smoke: error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
