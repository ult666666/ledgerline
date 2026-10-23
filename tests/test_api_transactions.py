"""Journal transactions, transfers and reversals through the HTTP API."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.support import Json, balance, deposit, entry, open_account, post, problem_type


@pytest.fixture
def cash(client: TestClient) -> Json:
    return open_account(client, name="Cash at bank", account_type="asset")


@pytest.fixture
def alice(client: TestClient) -> Json:
    return open_account(client, name="Alice wallet", account_type="liability")


@pytest.fixture
def bob(client: TestClient) -> Json:
    return open_account(client, name="Bob wallet", account_type="liability")


def transfer(
    client: TestClient, source: Json, destination: Json, amount: int, **extra: object
) -> Json:
    body = {
        "from_account_id": source["id"],
        "to_account_id": destination["id"],
        "amount": amount,
        "currency": source["currency"],
        **extra,
    }
    response = post(client, "/v1/transfers", body)
    assert response.status_code == 201, response.text
    result: Json = response.json()
    return result


class TestPostTransaction:
    def test_deposit_grows_both_sides(self, client: TestClient, cash: Json, alice: Json) -> None:
        response = post(
            client,
            "/v1/transactions",
            {
                "description": "Card top-up",
                "metadata": {"source": "test"},
                "entries": [entry(cash, 10_000), entry(alice, -10_000)],
            },
        )
        assert response.status_code == 201
        transaction = response.json()
        assert transaction["description"] == "Card top-up"
        assert transaction["metadata"] == {"source": "test"}
        assert transaction["entries"] == [entry(cash, 10_000), entry(alice, -10_000)]
        assert transaction["reverses_transaction_id"] is None
        assert transaction["reversed_by_transaction_id"] is None
        assert balance(client, cash) == 10_000
        assert balance(client, alice) == 10_000

    def test_multi_currency_transaction_balances_per_currency(self, client: TestClient) -> None:
        usd_cash = open_account(client, currency="USD")
        eur_cash = open_account(client, currency="EUR")
        usd_fx = open_account(client, account_type="equity", currency="USD", allow_negative=True)
        eur_fx = open_account(client, account_type="equity", currency="EUR", allow_negative=True)
        response = post(
            client,
            "/v1/transactions",
            {
                "description": "Buy 92 EUR for 100 USD",
                "entries": [
                    entry(usd_fx, 100),
                    entry(usd_cash, -100),
                    entry(eur_cash, 92),
                    entry(eur_fx, -92),
                ],
            },
        )
        # usd_cash starts at zero and may not go negative.
        assert response.status_code == 422
        assert problem_type(response) == "insufficient-funds"

        deposit(client, usd_cash, open_account(client, account_type="liability"), 100)
        response = post(
            client,
            "/v1/transactions",
            {
                "entries": [
                    entry(usd_fx, 100),
                    entry(usd_cash, -100),
                    entry(eur_cash, 92),
                    entry(eur_fx, -92),
                ]
            },
        )
        assert response.status_code == 201, response.text
        assert balance(client, usd_cash) == 0
        assert balance(client, eur_cash) == 92

    def test_rejects_unbalanced_entries(self, client: TestClient, cash: Json, alice: Json) -> None:
        response = post(
            client, "/v1/transactions", {"entries": [entry(cash, 100), entry(alice, -99)]}
        )
        assert response.status_code == 422
        assert problem_type(response) == "unbalanced-transaction"
        assert response.json()["imbalances"] == {"USD": 1}

    @pytest.mark.parametrize(
        ("amounts", "message"),
        [
            ([100], "at least two entries"),
            ([100, 0, -100], "must not be zero"),
        ],
    )
    def test_rejects_malformed_postings(
        self, client: TestClient, amounts: list[int], message: str
    ) -> None:
        accounts = [open_account(client, allow_negative=True) for _ in amounts]
        response = post(
            client,
            "/v1/transactions",
            {"entries": [entry(a, amount) for a, amount in zip(accounts, amounts, strict=True)]},
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-transaction"
        assert message in response.json()["detail"]

    def test_rejects_the_same_account_twice(
        self, client: TestClient, cash: Json, alice: Json
    ) -> None:
        response = post(
            client,
            "/v1/transactions",
            {"entries": [entry(cash, 100), entry(cash, 100), entry(alice, -200)]},
        )
        assert response.status_code == 422
        assert "appears more than once" in response.json()["detail"]

    def test_rejects_an_entry_in_the_wrong_currency(self, client: TestClient, cash: Json) -> None:
        euros = open_account(client, currency="EUR", account_type="liability")
        response = post(
            client,
            "/v1/transactions",
            {
                "entries": [
                    entry(cash, 100),
                    {"account_id": euros["id"], "amount": -100, "currency": "USD"},
                ]
            },
        )
        assert response.status_code == 422
        assert "does not match account" in response.json()["detail"]

    def test_unknown_account_is_404(self, client: TestClient, cash: Json) -> None:
        missing = {"account_id": str(uuid4()), "amount": -100, "currency": "USD"}
        response = post(client, "/v1/transactions", {"entries": [entry(cash, 100), missing]})
        assert response.status_code == 404
        assert response.json()["account_id"] == missing["account_id"]

    @pytest.mark.parametrize("amount", [100.0, 99.5, "100", True])
    def test_amounts_must_be_json_integers(
        self, client: TestClient, cash: Json, alice: Json, amount: object
    ) -> None:
        response = post(
            client,
            "/v1/transactions",
            {"entries": [{**entry(cash, 0), "amount": amount}, entry(alice, -100)]},
        )
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"

    def test_bounds_the_number_of_entries(self, client: TestClient, cash: Json) -> None:
        response = post(client, "/v1/transactions", {"entries": [entry(cash, 1)] * 101})
        assert response.status_code == 422

    def test_get_transaction(self, client: TestClient, cash: Json, alice: Json) -> None:
        created = deposit(client, cash, alice, 500)
        response = client.get(f"/v1/transactions/{created['id']}")
        assert response.status_code == 200
        assert response.json() == created

    def test_get_unknown_transaction_is_404(self, client: TestClient) -> None:
        response = client.get(f"/v1/transactions/{uuid4()}")
        assert response.status_code == 404
        assert problem_type(response) == "not-found"


class TestTransfers:
    def test_moves_money_between_wallets(
        self, client: TestClient, cash: Json, alice: Json, bob: Json
    ) -> None:
        deposit(client, cash, alice, 10_000)
        result = transfer(client, alice, bob, 2_500, description="Dinner", metadata={"k": "v"})
        assert result["description"] == "Dinner"
        assert result["metadata"] == {"k": "v"}
        # Wallets are credit-normal: the source is debited, the destination credited.
        assert result["entries"] == [entry(alice, 2_500), entry(bob, -2_500)]
        assert balance(client, alice) == 7_500
        assert balance(client, bob) == 2_500
        assert balance(client, cash) == 10_000

    def test_refuses_to_overdraw_and_changes_nothing(
        self, client: TestClient, cash: Json, alice: Json, bob: Json
    ) -> None:
        deposit(client, cash, alice, 1_000)
        response = post(
            client,
            "/v1/transfers",
            {
                "from_account_id": alice["id"],
                "to_account_id": bob["id"],
                "amount": 1_001,
                "currency": "USD",
            },
        )
        assert response.status_code == 422
        assert problem_type(response) == "insufficient-funds"
        assert response.json()["balance"] == 1_000
        assert balance(client, alice) == 1_000
        assert balance(client, bob) == 0

    def test_allow_negative_accounts_can_go_below_zero(
        self, client: TestClient, alice: Json
    ) -> None:
        funding = open_account(
            client, name="Promotions", account_type="liability", allow_negative=True
        )
        transfer(client, funding, alice, 500)
        assert balance(client, funding) == -500
        assert balance(client, alice) == 500

    def test_requires_a_shared_normal_balance(
        self, client: TestClient, cash: Json, alice: Json
    ) -> None:
        response = post(
            client,
            "/v1/transfers",
            {
                "from_account_id": cash["id"],
                "to_account_id": alice["id"],
                "amount": 1,
                "currency": "USD",
            },
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-transfer"

    def test_rejects_a_transfer_to_the_same_account(self, client: TestClient, alice: Json) -> None:
        response = post(
            client,
            "/v1/transfers",
            {
                "from_account_id": alice["id"],
                "to_account_id": alice["id"],
                "amount": 1,
                "currency": "USD",
            },
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-transfer"

    def test_rejects_a_currency_the_accounts_do_not_hold(
        self, client: TestClient, alice: Json, bob: Json
    ) -> None:
        response = post(
            client,
            "/v1/transfers",
            {
                "from_account_id": alice["id"],
                "to_account_id": bob["id"],
                "amount": 1,
                "currency": "EUR",
            },
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-transaction"

    @pytest.mark.parametrize("amount", [0, -1])
    def test_amount_must_be_positive(
        self, client: TestClient, alice: Json, bob: Json, amount: int
    ) -> None:
        response = post(
            client,
            "/v1/transfers",
            {
                "from_account_id": alice["id"],
                "to_account_id": bob["id"],
                "amount": amount,
                "currency": "USD",
            },
        )
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"


class TestReversals:
    def test_reversal_restores_balances_and_links_both_transactions(
        self, client: TestClient, cash: Json, alice: Json, bob: Json
    ) -> None:
        deposit(client, cash, alice, 10_000)
        original = transfer(client, alice, bob, 2_500)

        response = post(client, f"/v1/transactions/{original['id']}/reverse", None)
        assert response.status_code == 201
        reversal = response.json()
        assert reversal["reverses_transaction_id"] == original["id"]
        assert reversal["description"] == f"Reversal of {original['id']}"
        assert reversal["entries"] == [entry(alice, -2_500), entry(bob, 2_500)]
        assert balance(client, alice) == 10_000
        assert balance(client, bob) == 0

        reread = client.get(f"/v1/transactions/{original['id']}").json()
        assert reread["reversed_by_transaction_id"] == reversal["id"]
        # The original itself is untouched: corrections are new entries.
        assert reread["entries"] == original["entries"]

    def test_reversal_accepts_a_description_and_metadata(
        self, client: TestClient, cash: Json, alice: Json
    ) -> None:
        original = deposit(client, cash, alice, 100)
        response = post(
            client,
            f"/v1/transactions/{original['id']}/reverse",
            {"description": "Chargeback", "metadata": {"case": "42"}},
        )
        assert response.status_code == 201
        assert response.json()["description"] == "Chargeback"
        assert response.json()["metadata"] == {"case": "42"}

    def test_a_transaction_can_only_be_reversed_once(
        self, client: TestClient, cash: Json, alice: Json
    ) -> None:
        original = deposit(client, cash, alice, 100)
        first = post(client, f"/v1/transactions/{original['id']}/reverse", None)
        second = post(client, f"/v1/transactions/{original['id']}/reverse", None)
        assert first.status_code == 201
        assert second.status_code == 409
        assert problem_type(second) == "already-reversed"
        assert second.json()["reversed_by_transaction_id"] == first.json()["id"]

    def test_a_reversal_cannot_itself_be_reversed(
        self, client: TestClient, cash: Json, alice: Json
    ) -> None:
        original = deposit(client, cash, alice, 100)
        reversal = post(client, f"/v1/transactions/{original['id']}/reverse", None).json()
        response = post(client, f"/v1/transactions/{reversal['id']}/reverse", None)
        assert response.status_code == 422
        assert problem_type(response) == "reversal-not-allowed"

    def test_reversal_obeys_the_overdraft_rule(
        self, client: TestClient, cash: Json, alice: Json, bob: Json
    ) -> None:
        funding = deposit(client, cash, alice, 1_000)
        transfer(client, alice, bob, 1_000)  # Alice has spent everything
        response = post(client, f"/v1/transactions/{funding['id']}/reverse", None)
        assert response.status_code == 422
        assert problem_type(response) == "insufficient-funds"
        assert balance(client, alice) == 0

    def test_reversing_an_unknown_transaction_is_404(self, client: TestClient) -> None:
        response = post(client, f"/v1/transactions/{uuid4()}/reverse", None)
        assert response.status_code == 404
