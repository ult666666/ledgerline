"""Idempotency-Key semantics for money-moving endpoints."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Engine

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.idempotency import request_fingerprint
from ledgerline.schemas import ReversalCreate
from tests.support import Json, balance, deposit, open_account, post, problem_type


@pytest.fixture
def wallets(client: TestClient) -> tuple[Json, Json]:
    cash = open_account(client, name="Cash")
    alice = open_account(client, name="Alice", account_type="liability")
    bob = open_account(client, name="Bob", account_type="liability")
    deposit(client, cash, alice, 10_000)
    return alice, bob


def transfer_body(source: Json, destination: Json, amount: int) -> Json:
    return {
        "from_account_id": source["id"],
        "to_account_id": destination["id"],
        "amount": amount,
        "currency": "USD",
    }


def statement_length(client: TestClient, account: Json) -> int:
    return len(client.get(f"/v1/accounts/{account['id']}/entries").json()["data"])


def test_money_moving_endpoints_require_a_key(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    response = client.post("/v1/transfers", json=transfer_body(alice, bob, 1))
    assert response.status_code == 400
    assert problem_type(response) == "invalid-idempotency-key"
    entries = [
        {"account_id": alice["id"], "amount": 1, "currency": "USD"},
        {"account_id": bob["id"], "amount": -1, "currency": "USD"},
    ]
    response = client.post("/v1/transactions", json={"entries": entries})
    assert response.status_code == 400
    assert balance(client, bob) == 0


def test_reversal_requires_a_key(client: TestClient, wallets: tuple[Json, Json]) -> None:
    alice, _ = wallets
    original = client.get(f"/v1/accounts/{alice['id']}/entries").json()["data"][0]
    response = client.post(f"/v1/transactions/{original['transaction_id']}/reverse")
    assert response.status_code == 400


@pytest.mark.parametrize("key", [b"has space", b"x" * 256, "café".encode()])
def test_rejects_malformed_keys(client: TestClient, key: bytes) -> None:
    response = client.post(
        "/v1/accounts",
        json={"name": "Cash", "type": "asset", "currency": "USD"},
        headers={b"Idempotency-Key": key},
    )
    assert response.status_code == 400
    assert problem_type(response) == "invalid-idempotency-key"


def test_replay_returns_the_original_response_without_moving_money_again(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    body = transfer_body(alice, bob, 2_500)
    first = post(client, "/v1/transfers", body, key="pay-bob-1")
    replay = post(client, "/v1/transfers", body, key="pay-bob-1")

    assert first.status_code == replay.status_code == 201
    assert "Idempotent-Replayed" not in first.headers
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert balance(client, alice) == 7_500
    assert balance(client, bob) == 2_500
    assert statement_length(client, bob) == 1


def test_replay_matches_on_meaning_not_formatting(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    body = transfer_body(alice, bob, 100)
    headers = {"Idempotency-Key": "formatting", "Content-Type": "application/json"}
    first = client.post("/v1/transfers", content=json.dumps(body), headers=headers)
    reordered = dict(reversed(list(body.items())))
    retry = client.post("/v1/transfers", content=json.dumps(reordered, indent=4), headers=headers)
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json() == first.json()


def test_same_key_with_a_different_body_is_rejected(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    post(client, "/v1/transfers", transfer_body(alice, bob, 2_500), key="pay-bob-2")
    response = post(client, "/v1/transfers", transfer_body(alice, bob, 9_999), key="pay-bob-2")
    assert response.status_code == 409
    assert problem_type(response) == "idempotency-key-reused"
    assert balance(client, alice) == 7_500


def test_same_key_on_a_different_endpoint_is_rejected(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    post(client, "/v1/transfers", transfer_body(alice, bob, 1), key="shared")
    response = post(
        client,
        "/v1/accounts",
        {"name": "Other", "type": "asset", "currency": "USD"},
        key="shared",
    )
    assert response.status_code == 409


def test_a_failed_request_does_not_use_up_its_key(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    body = transfer_body(bob, alice, 500)
    refused = post(client, "/v1/transfers", body, key="bob-pays-alice")
    assert refused.status_code == 422  # Bob has no money yet

    post(client, "/v1/transfers", transfer_body(alice, bob, 500))
    retried = post(client, "/v1/transfers", body, key="bob-pays-alice")
    assert retried.status_code == 201
    assert "Idempotent-Replayed" not in retried.headers


def test_reversal_replay_returns_the_original_reversal(
    client: TestClient, wallets: tuple[Json, Json]
) -> None:
    alice, bob = wallets
    original = post(client, "/v1/transfers", transfer_body(alice, bob, 300)).json()
    path = f"/v1/transactions/{original['id']}/reverse"
    first = post(client, path, None, key="undo-1")
    replay = post(client, path, {}, key="undo-1")  # an empty body means the same as no body
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()


def test_keys_are_scoped_per_api_client(
    db: Engine, settings: Settings, wallets: tuple[Json, Json]
) -> None:
    api_keys = [*settings.api_keys, "second-client-key"]
    two_clients = create_app(settings.model_copy(update={"api_keys": api_keys}), engine=db)
    alice, bob = wallets
    body = transfer_body(alice, bob, 100)
    with TestClient(two_clients) as client:
        responses = [
            client.post(
                "/v1/transfers",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "same-key"},
            )
            for api_key in api_keys
        ]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] != responses[1].json()["id"]
    assert not any("Idempotent-Replayed" in response.headers for response in responses)


class _Body(BaseModel):
    amount: int


def test_fingerprint_covers_method_path_and_body() -> None:
    base = request_fingerprint("POST", "/v1/transfers", _Body(amount=1))
    assert base == request_fingerprint("post", "/v1/transfers", _Body(amount=1))
    assert base != request_fingerprint("POST", "/v1/transfers", _Body(amount=2))
    assert base != request_fingerprint("POST", "/v1/transactions", _Body(amount=1))
    assert request_fingerprint("POST", "/r", None) != request_fingerprint(
        "POST", "/r", ReversalCreate()
    )
