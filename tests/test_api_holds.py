"""Holds (two-phase transfers) through the HTTP API: authorize, capture, void and expiry.

These tests use ``clocked_client``, whose server reads a fake clock, so expiry
is tested exactly at its boundary instead of by sleeping.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from ledgerline.domain import MAX_MAGNITUDE
from tests.support import (
    FakeClock,
    Json,
    account_balances,
    authorize,
    deposit,
    entry,
    hold_body,
    open_account,
    post,
    problem_type,
)

HOUR = timedelta(hours=1)


@pytest.fixture
def cash(clocked_client: TestClient) -> Json:
    return open_account(clocked_client, name="Cash at bank")


@pytest.fixture
def alice(clocked_client: TestClient, cash: Json) -> Json:
    """A wallet holding 10,000 that may not go negative."""
    wallet = open_account(clocked_client, name="Alice wallet", account_type="liability")
    deposit(clocked_client, cash, wallet, 10_000)
    return wallet


@pytest.fixture
def bob(clocked_client: TestClient) -> Json:
    return open_account(clocked_client, name="Bob wallet", account_type="liability")


def capture(
    client: TestClient, hold: Json, body: Json | None = None, *, key: str | None = None
) -> httpx2.Response:
    return post(client, f"/v1/holds/{hold['id']}/capture", body, key=key)


def void(client: TestClient, hold: Json, *, key: str | None = None) -> httpx2.Response:
    return post(client, f"/v1/holds/{hold['id']}/void", None, key=key)


def statement(client: TestClient, account: Json) -> list[Json]:
    lines: list[Json] = client.get(f"/v1/accounts/{account['id']}/entries").json()["data"]
    return lines


def get_hold(client: TestClient, hold: Json) -> Json:
    response = client.get(f"/v1/holds/{hold['id']}")
    assert response.status_code == 200, response.text
    body: Json = response.json()
    return body


class TestAuthorize:
    def test_reserves_funds_without_posting_anything(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        response = post(
            clocked_client,
            "/v1/holds",
            hold_body(
                alice, bob, 4_000, expires_at=expires_at, description="Ride", metadata={"r": "1"}
            ),
        )
        assert response.status_code == 201, response.text
        hold = response.json()
        assert hold["status"] == "active"
        assert (hold["amount"], hold["captured_amount"], hold["currency"]) == (4_000, None, "USD")
        assert (hold["from_account_id"], hold["to_account_id"]) == (alice["id"], bob["id"])
        assert (hold["description"], hold["metadata"]) == ("Ride", {"r": "1"})
        assert hold["closed_at"] is None
        assert hold["capture_transaction_id"] is None
        assert datetime.fromisoformat(hold["created_at"]) == clock.now
        assert datetime.fromisoformat(hold["expires_at"]) == expires_at
        # The ledger balance only counts postings; the hold lowers what is available.
        assert account_balances(clocked_client, alice) == (10_000, 4_000, 6_000)
        assert account_balances(clocked_client, bob) == (0, 0, 0)
        assert len(statement(clocked_client, alice)) == 1  # just the deposit
        assert get_hold(clocked_client, hold) == hold

    def test_balance_and_account_list_report_held_and_available(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        authorize(clocked_client, alice, bob, 2_500, expires_at=clock.now + HOUR)
        balance = clocked_client.get(f"/v1/accounts/{alice['id']}/balance").json()
        assert (balance["balance"], balance["held_amount"], balance["available_balance"]) == (
            10_000,
            2_500,
            7_500,
        )
        listed = {a["id"]: a for a in clocked_client.get("/v1/accounts").json()["data"]}
        assert (listed[alice["id"]]["held_amount"], listed[alice["id"]]["available_balance"]) == (
            2_500,
            7_500,
        )
        assert (listed[bob["id"]]["held_amount"], listed[bob["id"]]["available_balance"]) == (0, 0)

    def test_holds_can_reserve_exactly_the_available_balance_and_no_more(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        authorize(clocked_client, alice, bob, 6_000, expires_at=expires_at)
        authorize(clocked_client, alice, bob, 4_000, expires_at=expires_at)
        response = post(
            clocked_client, "/v1/holds", hold_body(alice, bob, 1, expires_at=expires_at)
        )
        assert response.status_code == 422
        assert problem_type(response) == "insufficient-funds"
        problem = response.json()
        assert (problem["balance"], problem["held_amount"], problem["available_balance"]) == (
            10_000,
            10_000,
            0,
        )
        assert problem["change"] == -1
        assert account_balances(clocked_client, alice) == (10_000, 10_000, 0)

    def test_transfers_cannot_spend_held_funds(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)

        def pay_bob(amount: int) -> httpx2.Response:
            body = {
                "from_account_id": alice["id"],
                "to_account_id": bob["id"],
                "amount": amount,
                "currency": "USD",
            }
            return post(clocked_client, "/v1/transfers", body)

        refused = pay_bob(6_001)
        assert refused.status_code == 422
        assert problem_type(refused) == "insufficient-funds"
        assert "of which 4000 is reserved by active holds" in refused.json()["detail"]
        assert pay_bob(6_000).status_code == 201
        assert account_balances(clocked_client, alice) == (4_000, 4_000, 0)

    def test_journal_postings_cannot_spend_held_funds(
        self, clocked_client: TestClient, clock: FakeClock, cash: Json, alice: Json, bob: Json
    ) -> None:
        authorize(clocked_client, alice, bob, 9_000, expires_at=clock.now + HOUR)
        # A withdrawal: credit cash, debit (shrink) Alice's wallet.
        response = post(
            clocked_client,
            "/v1/transactions",
            {"entries": [entry(cash, -1_001), entry(alice, 1_001)]},
        )
        assert response.status_code == 422
        assert problem_type(response) == "insufficient-funds"
        assert response.json()["available_balance"] == 1_000

    def test_an_account_that_may_go_negative_can_hold_more_than_its_balance(
        self, clocked_client: TestClient, clock: FakeClock, bob: Json
    ) -> None:
        promotions = open_account(
            clocked_client, name="Promotions", account_type="liability", allow_negative=True
        )
        authorize(clocked_client, promotions, bob, 5_000, expires_at=clock.now + HOUR)
        assert account_balances(clocked_client, promotions) == (0, 5_000, -5_000)

    def test_holds_never_take_balances_outside_the_json_safe_range(
        self, clocked_client: TestClient, clock: FakeClock, bob: Json
    ) -> None:
        # Funds do not limit an account that may go negative, so without a range
        # check its holds could stack up without end: held_amount past 2**53 - 1,
        # and on SQLite a SUM that overflows 64 bits after about 1,025 of them.
        promotions = open_account(
            clocked_client, name="Promotions", account_type="liability", allow_negative=True
        )
        authorize(clocked_client, promotions, bob, MAX_MAGNITUDE, expires_at=clock.now + HOUR)
        response = post(
            clocked_client,
            "/v1/holds",
            hold_body(promotions, bob, 1, expires_at=clock.now + HOUR),
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-transaction"
        assert "available balance would leave the supported range" in response.json()["detail"]
        assert account_balances(clocked_client, promotions) == (0, MAX_MAGNITUDE, -MAX_MAGNITUDE)

    @pytest.mark.parametrize(
        "expires_at",
        # Valid ISO 8601 with an offset, but outside the years 1 to 9999 in UTC.
        ["9999-12-31T23:00:00-05:00", "0001-01-01T00:30:00+01:00"],
    )
    def test_expires_at_must_be_representable_in_utc(
        self,
        clocked_client: TestClient,
        clock: FakeClock,
        alice: Json,
        bob: Json,
        expires_at: str,
    ) -> None:
        body = {**hold_body(alice, bob, 100, expires_at=clock.now + HOUR), "expires_at": expires_at}
        response = post(clocked_client, "/v1/holds", body)
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"amount": 0},
            {"amount": -1},
            {"amount": 12.5},
            {"amount": "100"},
            {"expires_at": None},
            {"expires_at": "2026-11-02T10:00:00"},  # no timezone
            {"currency": "usd"},
            {"unexpected": True},
        ],
    )
    def test_validates_the_request(
        self,
        clocked_client: TestClient,
        clock: FakeClock,
        alice: Json,
        bob: Json,
        overrides: Json,
    ) -> None:
        body = {**hold_body(alice, bob, 100, expires_at=clock.now + HOUR), **overrides}
        if body["expires_at"] is None:
            del body["expires_at"]
        response = post(clocked_client, "/v1/holds", body)
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"

    @pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
    def test_expires_at_must_be_in_the_future(
        self,
        clocked_client: TestClient,
        clock: FakeClock,
        alice: Json,
        bob: Json,
        offset: timedelta,
    ) -> None:
        response = post(
            clocked_client, "/v1/holds", hold_body(alice, bob, 100, expires_at=clock.now + offset)
        )
        assert response.status_code == 422
        assert problem_type(response) == "invalid-hold"
        assert datetime.fromisoformat(response.json()["server_time"]) == clock.now
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)

    def test_follows_the_transfer_rules(
        self, clocked_client: TestClient, clock: FakeClock, cash: Json, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        euros = open_account(clocked_client, account_type="liability", currency="EUR")
        cases = [
            (hold_body(alice, alice, 100, expires_at=expires_at), 422, "invalid-transfer"),
            # A wallet and a cash account grow on different sides.
            (hold_body(alice, cash, 100, expires_at=expires_at), 422, "invalid-transfer"),
            (hold_body(alice, euros, 100, expires_at=expires_at), 422, "invalid-transaction"),
            (
                {
                    **hold_body(alice, bob, 100, expires_at=expires_at),
                    "to_account_id": str(uuid4()),
                },
                404,
                "not-found",
            ),
        ]
        for body, status, kind in cases:
            response = post(clocked_client, "/v1/holds", body)
            assert (response.status_code, problem_type(response)) == (status, kind), body
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)

    def test_unknown_hold_is_404(self, clocked_client: TestClient) -> None:
        missing = uuid4()
        response = clocked_client.get(f"/v1/holds/{missing}")
        assert response.status_code == 404
        assert response.json()["hold_id"] == str(missing)
        for path in (f"/v1/holds/{missing}/capture", f"/v1/holds/{missing}/void"):
            assert post(clocked_client, path, None).status_code == 404


class TestCapture:
    def test_full_capture_posts_the_transfer_and_releases_the_hold(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        clock.advance(minutes=5)

        response = capture(clocked_client, hold)
        assert response.status_code == 200, response.text
        captured = response.json()
        assert captured["status"] == "captured"
        assert captured["captured_amount"] == 4_000
        assert datetime.fromisoformat(captured["closed_at"]) == clock.now
        assert account_balances(clocked_client, alice) == (6_000, 0, 6_000)
        assert account_balances(clocked_client, bob) == (4_000, 0, 4_000)

        transaction = clocked_client.get(
            f"/v1/transactions/{captured['capture_transaction_id']}"
        ).json()
        assert transaction["captures_hold_id"] == hold["id"]
        assert transaction["description"] == f"Capture of hold {hold['id']}"
        assert transaction["posted_at"] == captured["closed_at"]
        # Wallets are credit-normal: the source is debited, the destination credited.
        assert transaction["entries"] == [entry(alice, 4_000), entry(bob, -4_000)]
        assert get_hold(clocked_client, hold) == captured

    def test_partial_capture_releases_the_remainder(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        other = authorize(clocked_client, alice, bob, 1_000, expires_at=clock.now + HOUR)
        response = capture(clocked_client, hold, {"amount": 3_200})
        assert response.status_code == 200, response.text
        assert response.json()["captured_amount"] == 3_200
        # 800 of the first hold is released; the other hold still counts.
        assert account_balances(clocked_client, alice) == (6_800, 1_000, 5_800)
        assert account_balances(clocked_client, bob) == (3_200, 0, 3_200)
        assert get_hold(clocked_client, other)["status"] == "active"

    def test_a_hold_on_the_whole_balance_can_be_captured_in_full(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        # The funds check on the capture must not count the hold being captured:
        # its funds are the posting itself.
        hold = authorize(clocked_client, alice, bob, 10_000, expires_at=clock.now + HOUR)
        assert account_balances(clocked_client, alice) == (10_000, 10_000, 0)
        response = capture(clocked_client, hold)
        assert response.status_code == 200, response.text
        assert account_balances(clocked_client, alice) == (0, 0, 0)
        assert account_balances(clocked_client, bob) == (10_000, 0, 10_000)

    def test_capture_takes_a_description_and_metadata_for_the_posting(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 500, expires_at=clock.now + HOUR)
        captured = capture(
            clocked_client, hold, {"description": "Ride r-1", "metadata": {"ride": "r-1"}}
        ).json()
        transaction = clocked_client.get(
            f"/v1/transactions/{captured['capture_transaction_id']}"
        ).json()
        assert (transaction["description"], transaction["metadata"]) == (
            "Ride r-1",
            {"ride": "r-1"},
        )

    def test_cannot_capture_more_than_was_authorized(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        response = capture(clocked_client, hold, {"amount": 4_001})
        assert response.status_code == 422
        assert problem_type(response) == "invalid-hold"
        assert (response.json()["authorized_amount"], response.json()["requested_amount"]) == (
            4_000,
            4_001,
        )
        assert get_hold(clocked_client, hold)["status"] == "active"
        assert account_balances(clocked_client, alice) == (10_000, 4_000, 6_000)

    @pytest.mark.parametrize("amount", [0, -5, 1.5])
    def test_capture_amount_must_be_a_positive_integer(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json, amount: object
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        response = capture(clocked_client, hold, {"amount": amount})
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"

    def test_a_hold_is_captured_at_most_once(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        first = capture(clocked_client, hold, {"amount": 1_000})
        assert first.status_code == 200
        for second in (capture(clocked_client, hold), void(clocked_client, hold)):
            assert second.status_code == 409
            assert problem_type(second) == "hold-not-active"
            assert second.json()["hold_status"] == "captured"
            assert second.json()["status"] == 409
        assert account_balances(clocked_client, alice) == (9_000, 0, 9_000)
        assert [line["amount"] for line in statement(clocked_client, bob)] == [-1_000]

    def test_a_capture_can_be_refunded_by_reversing_its_transaction(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        captured = capture(clocked_client, hold).json()
        refund = post(
            clocked_client, f"/v1/transactions/{captured['capture_transaction_id']}/reverse", None
        )
        assert refund.status_code == 201
        assert refund.json()["captures_hold_id"] is None
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)
        assert account_balances(clocked_client, bob) == (0, 0, 0)
        # The hold's own history does not change: it was captured, then refunded.
        assert get_hold(clocked_client, hold)["status"] == "captured"


class TestVoid:
    def test_void_releases_the_whole_hold_and_posts_nothing(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        clock.advance(seconds=30)
        response = void(clocked_client, hold)
        assert response.status_code == 200, response.text
        voided = response.json()
        assert voided["status"] == "voided"
        assert voided["captured_amount"] is None
        assert voided["capture_transaction_id"] is None
        assert datetime.fromisoformat(voided["closed_at"]) == clock.now
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)
        assert statement(clocked_client, bob) == []

    def test_a_voided_hold_cannot_be_captured_or_voided_again(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        assert void(clocked_client, hold).status_code == 200
        for again in (capture(clocked_client, hold), void(clocked_client, hold)):
            assert again.status_code == 409
            assert problem_type(again) == "hold-not-active"
            assert again.json()["hold_status"] == "voided"
        assert account_balances(clocked_client, bob) == (0, 0, 0)


class TestExpiry:
    def test_a_hold_counts_until_expires_at_and_not_from_then_on(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=expires_at)

        clock.now = expires_at - timedelta(microseconds=1)
        assert get_hold(clocked_client, hold)["status"] == "active"
        assert account_balances(clocked_client, alice) == (10_000, 4_000, 6_000)

        clock.now = expires_at
        assert get_hold(clocked_client, hold)["status"] == "expired"
        assert account_balances(clocked_client, alice) == (10_000, 0, 10_000)
        # The freed funds can be held again straight away.
        authorize(clocked_client, alice, bob, 10_000, expires_at=expires_at + HOUR)

    def test_an_expired_hold_cannot_be_captured_or_voided(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=expires_at)
        clock.now = expires_at
        for attempt in (capture(clocked_client, hold), void(clocked_client, hold)):
            assert attempt.status_code == 409
            assert problem_type(attempt) == "hold-expired"
            assert attempt.json()["hold_status"] == "expired"
            assert datetime.fromisoformat(attempt.json()["expires_at"]) == expires_at
        assert account_balances(clocked_client, bob) == (0, 0, 0)
        assert get_hold(clocked_client, hold)["closed_at"] is None

    def test_a_capture_just_before_expiry_succeeds(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        expires_at = clock.now + HOUR
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=expires_at)
        clock.now = expires_at - timedelta(microseconds=1)
        assert capture(clocked_client, hold).status_code == 200
        clock.now = expires_at + HOUR
        assert get_hold(clocked_client, hold)["status"] == "captured"  # final, whatever the time

    def test_balance_as_of_reports_the_holds_active_at_that_time(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        start = clock.advance(minutes=1)  # the deposit was posted a minute earlier
        captured_hold = authorize(clocked_client, alice, bob, 4_000, expires_at=start + HOUR)
        clock.advance(minutes=1)
        authorize(clocked_client, alice, bob, 1_000, expires_at=start + timedelta(minutes=3))
        clock.advance(minutes=1)
        capture(clocked_client, captured_hold, {"amount": 3_000})
        clock.advance(minutes=10)

        def at(moment: datetime) -> tuple[int, int, int]:
            response = clocked_client.get(
                f"/v1/accounts/{alice['id']}/balance", params={"as_of": moment.isoformat()}
            )
            body = response.json()
            return body["balance"], body["held_amount"], body["available_balance"]

        second = timedelta(seconds=1)
        assert at(start - second) == (10_000, 0, 10_000)  # before any hold
        assert at(start) == (10_000, 4_000, 6_000)  # the first hold, from its creation
        assert at(start + timedelta(minutes=1)) == (10_000, 5_000, 5_000)  # both holds
        assert at(start + timedelta(minutes=2)) == (7_000, 1_000, 6_000)  # captured 3,000
        assert at(start + timedelta(minutes=3)) == (7_000, 0, 7_000)  # second hold expired
        assert at(clock.now) == account_balances(clocked_client, alice)


class TestIdempotency:
    def test_hold_writes_require_a_key(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 100, expires_at=clock.now + HOUR)
        for path, body in [
            ("/v1/holds", hold_body(alice, bob, 100, expires_at=clock.now + HOUR)),
            (f"/v1/holds/{hold['id']}/capture", None),
            (f"/v1/holds/{hold['id']}/void", None),
        ]:
            response = clocked_client.post(path, json=body)
            assert response.status_code == 400, path
            assert problem_type(response) == "invalid-idempotency-key"
        assert account_balances(clocked_client, alice) == (10_000, 100, 9_900)

    def test_retries_replay_the_original_response(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        body = hold_body(alice, bob, 4_000, expires_at=clock.now + HOUR)
        created = post(clocked_client, "/v1/holds", body, key="hold-1")
        retried = post(clocked_client, "/v1/holds", body, key="hold-1")
        assert retried.status_code == 201
        assert retried.headers["Idempotent-Replayed"] == "true"
        assert retried.json() == created.json()
        assert account_balances(clocked_client, alice) == (10_000, 4_000, 6_000)

        hold = created.json()
        first = capture(clocked_client, hold, {"amount": 1_000}, key="capture-1")
        again = capture(clocked_client, hold, {"amount": 1_000}, key="capture-1")
        assert again.status_code == 200
        assert again.headers["Idempotent-Replayed"] == "true"
        assert again.json() == first.json()
        assert account_balances(clocked_client, alice) == (9_000, 0, 9_000)

        other = authorize(clocked_client, alice, bob, 500, expires_at=clock.now + HOUR)
        voided = void(clocked_client, other, key="void-1")
        assert void(clocked_client, other, key="void-1").json() == voided.json()

    def test_an_empty_capture_body_means_the_same_as_none(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        first = capture(clocked_client, hold, None, key="capture-all")
        replay = capture(clocked_client, hold, {}, key="capture-all")
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert replay.json() == first.json()

    def test_reusing_a_key_for_a_different_request_is_rejected(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        hold = authorize(clocked_client, alice, bob, 4_000, expires_at=clock.now + HOUR)
        assert capture(clocked_client, hold, {"amount": 100}, key="k").status_code == 200
        different = capture(clocked_client, hold, {"amount": 200}, key="k")
        assert different.status_code == 409
        assert problem_type(different) == "idempotency-key-reused"
        assert void(clocked_client, hold, key="k").status_code == 409

    def test_a_retry_after_expiry_still_replays_the_original_response(
        self, clocked_client: TestClient, clock: FakeClock, alice: Json, bob: Json
    ) -> None:
        body = hold_body(alice, bob, 4_000, expires_at=clock.now + HOUR)
        created = post(clocked_client, "/v1/holds", body, key="hold-late")
        clock.advance(hours=2)
        replay = post(clocked_client, "/v1/holds", body, key="hold-late")
        # A replay repeats the original outcome; the hold's current state is a GET away.
        assert replay.json() == created.json()
        assert replay.json()["status"] == "active"
        assert get_hold(clocked_client, created.json())["status"] == "expired"
