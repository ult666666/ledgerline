"""Helpers for driving the API from tests."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient

API_KEY = "test-key-not-secret"

Json = dict[str, Any]


class FakeClock:
    """A clock that only moves when a test moves it.

    The service reads it for every timestamp and every expiry decision, so
    tests can put a hold exactly at, just before or long after its expiry.
    """

    def __init__(self, start: datetime = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


def open_account(
    client: TestClient,
    *,
    name: str = "Account",
    account_type: str = "asset",
    currency: str = "USD",
    allow_negative: bool = False,
    **extra: Any,
) -> Json:
    response = client.post(
        "/v1/accounts",
        json={
            "name": name,
            "type": account_type,
            "currency": currency,
            "allow_negative": allow_negative,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    account: Json = response.json()
    return account


def post(
    client: TestClient, path: str, body: Json | None, *, key: str | None = None
) -> httpx2.Response:
    """POST with an Idempotency-Key header (a fresh one unless ``key`` is given)."""
    return client.post(path, json=body, headers={"Idempotency-Key": key or str(uuid4())})


def entry(account: Json, amount: int) -> Json:
    return {"account_id": account["id"], "amount": amount, "currency": account["currency"]}


def balance(client: TestClient, account: Json) -> int:
    response = client.get(f"/v1/accounts/{account['id']}/balance")
    assert response.status_code == 200, response.text
    value: int = response.json()["balance"]
    return value


def deposit(client: TestClient, cash: Json, wallet: Json, amount: int) -> Json:
    """Debit a cash (asset) account and credit a customer wallet (liability)."""
    response = post(
        client,
        "/v1/transactions",
        {"description": "Deposit", "entries": [entry(cash, amount), entry(wallet, -amount)]},
    )
    assert response.status_code == 201, response.text
    transaction: Json = response.json()
    return transaction


def problem_type(response: httpx2.Response) -> str:
    """The short problem type of an RFC 9457 error response, e.g. 'insufficient-funds'."""
    assert response.headers["content-type"] == "application/problem+json"
    value: str = response.json()["type"].rsplit("#", 1)[-1]
    return value


def hold_body(
    source: Json, destination: Json, amount: int, *, expires_at: datetime, **extra: Any
) -> Json:
    return {
        "from_account_id": source["id"],
        "to_account_id": destination["id"],
        "amount": amount,
        "currency": source["currency"],
        "expires_at": expires_at.isoformat(),
        **extra,
    }


def authorize(
    client: TestClient, source: Json, destination: Json, amount: int, *, expires_at: datetime
) -> Json:
    """Create a hold that is expected to succeed."""
    response = post(
        client, "/v1/holds", hold_body(source, destination, amount, expires_at=expires_at)
    )
    assert response.status_code == 201, response.text
    hold: Json = response.json()
    return hold


def account_balances(client: TestClient, account: Json) -> tuple[int, int, int]:
    """(balance, held_amount, available_balance) as GET /v1/accounts/{id} reports them."""
    response = client.get(f"/v1/accounts/{account['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    return body["balance"], body["held_amount"], body["available_balance"]
