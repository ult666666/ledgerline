"""Account endpoints: creation, listing, balances and statements."""

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.support import balance, deposit, open_account, post, problem_type


def test_open_account_defaults_the_normal_balance_from_the_type(client: TestClient) -> None:
    response = client.post(
        "/v1/accounts",
        json={"name": "Alice wallet", "type": "liability", "currency": "USD"},
    )
    assert response.status_code == 201
    account = response.json()
    assert account["normal_balance"] == "credit"
    assert account["allow_negative"] is False
    assert account["balance"] == 0
    assert account["metadata"] == {}
    assert datetime.fromisoformat(account["created_at"]).tzinfo is not None


def test_open_account_accepts_an_explicit_normal_balance(client: TestClient) -> None:
    # A contra-asset such as accumulated depreciation grows with credits.
    account = open_account(
        client, name="Accumulated depreciation", account_type="asset", normal_balance="credit"
    )
    assert account["normal_balance"] == "credit"


def test_open_account_keeps_metadata(client: TestClient) -> None:
    account = open_account(client, metadata={"customer_id": "cus_123"})
    assert account["metadata"] == {"customer_id": "cus_123"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": "usd"},
        {"currency": "US"},
        {"type": "piggy-bank"},
        {"name": "   "},
        {"allow_negative": "yes"},
        {"metadata": {"key": 1}},
        {"unexpected": True},
    ],
)
def test_open_account_validates_input(client: TestClient, overrides: dict[str, object]) -> None:
    body = {"name": "Cash", "type": "asset", "currency": "USD", **overrides}
    response = client.post("/v1/accounts", json=body)
    assert response.status_code == 422
    assert problem_type(response) == "validation-error"
    assert response.json()["errors"]


def test_get_account(client: TestClient) -> None:
    account = open_account(client, name="Cash")
    response = client.get(f"/v1/accounts/{account['id']}")
    assert response.status_code == 200
    assert response.json() == account


def test_get_unknown_account_is_404(client: TestClient) -> None:
    missing = uuid4()
    response = client.get(f"/v1/accounts/{missing}")
    assert response.status_code == 404
    assert problem_type(response) == "not-found"
    assert response.json()["account_id"] == str(missing)


def test_malformed_account_id_is_422(client: TestClient) -> None:
    response = client.get("/v1/accounts/not-a-uuid")
    assert response.status_code == 422


def test_list_accounts_pages_with_a_cursor(client: TestClient) -> None:
    created = [open_account(client, name=f"Account {n}")["id"] for n in range(5)]

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/v1/accounts", params=params)
        assert response.status_code == 200
        page = response.json()
        seen.extend(account["id"] for account in page["data"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == created  # oldest first, nothing skipped or repeated
    assert pages == 3


def test_list_accounts_rejects_a_forged_cursor(client: TestClient) -> None:
    for cursor in ("not-base64!", "e30", "bm90IGpzb24", "WzFd"):
        response = client.get("/v1/accounts", params={"cursor": cursor})
        assert response.status_code == 400, cursor
        assert problem_type(response) == "invalid-cursor"


@pytest.mark.parametrize("limit", [0, 101])
def test_list_accounts_bounds_the_page_size(client: TestClient, limit: int) -> None:
    assert client.get("/v1/accounts", params={"limit": limit}).status_code == 422


def test_open_account_with_an_idempotency_key_is_safe_to_retry(client: TestClient) -> None:
    body = {"name": "Cash", "type": "asset", "currency": "USD"}
    first = post(client, "/v1/accounts", body, key="open-cash")
    retry = post(client, "/v1/accounts", body, key="open-cash")
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(client.get("/v1/accounts").json()["data"]) == 1


def test_balance_as_of_reconstructs_history(client: TestClient) -> None:
    cash = open_account(client, name="Cash")
    wallet = open_account(client, name="Wallet", account_type="liability")
    first = deposit(client, cash, wallet, 1_000)
    second = deposit(client, cash, wallet, 500)

    def balance_at(timestamp: str) -> int:
        response = client.get(f"/v1/accounts/{wallet['id']}/balance", params={"as_of": timestamp})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["as_of"] == timestamp
        value: int = body["balance"]
        return value

    assert balance_at("2000-01-01T00:00:00Z") == 0
    assert balance_at(first["posted_at"]) == 1_000
    assert balance_at(second["posted_at"]) == 1_500
    assert balance(client, wallet) == 1_500


def test_balance_as_of_requires_a_timezone(client: TestClient) -> None:
    account = open_account(client)
    response = client.get(
        f"/v1/accounts/{account['id']}/balance", params={"as_of": "2026-01-01T00:00:00"}
    )
    assert response.status_code == 422


def test_balance_of_unknown_account_is_404(client: TestClient) -> None:
    assert client.get(f"/v1/accounts/{uuid4()}/balance").status_code == 404


def test_statement_lists_entries_with_running_balances(client: TestClient) -> None:
    cash = open_account(client, name="Cash")
    alice = open_account(client, name="Alice", account_type="liability")
    bob = open_account(client, name="Bob", account_type="liability")
    deposit(client, cash, alice, 10_000)
    transfer = post(
        client,
        "/v1/transfers",
        {
            "from_account_id": alice["id"],
            "to_account_id": bob["id"],
            "amount": 2_500,
            "currency": "USD",
            "description": "Dinner",
        },
    )
    assert transfer.status_code == 201

    lines = client.get(f"/v1/accounts/{alice['id']}/entries").json()["data"]
    assert [(line["amount"], line["balance_after"]) for line in lines] == [
        (-10_000, 10_000),  # credit: the wallet grows
        (2_500, 7_500),  # debit: the wallet shrinks
    ]
    assert lines[1]["description"] == "Dinner"
    assert lines[1]["transaction_id"] == transfer.json()["id"]


def test_statement_pages_with_a_cursor(client: TestClient) -> None:
    cash = open_account(client, name="Cash")
    wallet = open_account(client, name="Wallet", account_type="liability")
    for amount in (100, 200, 300):
        deposit(client, cash, wallet, amount)

    first = client.get(f"/v1/accounts/{wallet['id']}/entries", params={"limit": 2}).json()
    assert [line["balance_after"] for line in first["data"]] == [100, 300]
    second = client.get(
        f"/v1/accounts/{wallet['id']}/entries", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert [line["balance_after"] for line in second["data"]] == [600]
    assert second["next_cursor"] is None


def test_statement_rejects_a_forged_cursor(client: TestClient) -> None:
    account = open_account(client)
    response = client.get(f"/v1/accounts/{account['id']}/entries", params={"cursor": "e30"})
    assert response.status_code == 400


def test_statement_of_unknown_account_is_404(client: TestClient) -> None:
    assert client.get(f"/v1/accounts/{uuid4()}/entries").status_code == 404
