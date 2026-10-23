"""Helpers for driving the API from tests."""

from typing import Any
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient

API_KEY = "test-key-not-secret"

Json = dict[str, Any]


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
