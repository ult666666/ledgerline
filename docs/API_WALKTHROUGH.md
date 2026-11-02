# API walkthrough

A complete session against the ledgerline API, request by request. Back to the
[README](../README.md).

Everything below was run against a local server (`uv run ledgerline serve` on
SQLite, freshly migrated); the IDs and timestamps are from that run. It uses
`curl`, `jq` and GNU `date`. Steps 10 to 17 cover holds (two-phase transfers);
step 16 waits four seconds for a hold to expire.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/accounts`, `GET /v1/accounts`, `GET /v1/accounts/{id}` | Open, list (cursor-paginated) and read accounts |
| `GET /v1/accounts/{id}/balance?as_of=...` | Current or point-in-time balance |
| `GET /v1/accounts/{id}/entries` | Statement with running balances (cursor-paginated) |
| `POST /v1/transactions`, `GET /v1/transactions/{id}` | Post or read a journal transaction |
| `POST /v1/transfers` | Move money between two accounts with the same normal balance side |
| `POST /v1/transactions/{id}/reverse` | Reverse a transaction |
| `POST /v1/holds`, `GET /v1/holds/{id}` | Authorize (reserve funds for a later transfer) or read a hold |
| `POST /v1/holds/{id}/capture`, `POST /v1/holds/{id}/void` | Post all or part of a hold, or release it |
| `GET /healthz`, `GET /readyz` | Liveness and readiness (no authentication) |

**Sign convention.** Entry amounts are signed: positive is a debit, negative a
credit. Balances are reported in each account's own terms: assets grow with
debits, while liabilities such as a customer's wallet (money the platform owes
the customer) grow with credits.

**Three balances.** `balance` is the ledger balance: posted entries only.
`held_amount` is the total of the account's active holds, and
`available_balance` is `balance - held_amount`, the part the account can still
spend or hold.

## 1. Open accounts

The bank account holding customer money is an asset; each customer's wallet is
a liability.

```bash
api() { curl -s -H "Authorization: Bearer dev-key-change-me" -H "Content-Type: application/json" "$@"; }
BASE=http://127.0.0.1:8000/v1

CASH=$(api $BASE/accounts -d '{"name": "Cash at bank", "type": "asset", "currency": "USD"}' | jq -r .id)
ALICE=$(api $BASE/accounts -d '{"name": "Alice wallet", "type": "liability", "currency": "USD"}' | jq -r .id)
BOB=$(api $BASE/accounts -d '{"name": "Bob wallet", "type": "liability", "currency": "USD"}' | jq -r .id)
api $BASE/accounts/$ALICE | jq
```

```json
{
  "id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "name": "Alice wallet",
  "type": "liability",
  "currency": "USD",
  "normal_balance": "credit",
  "allow_negative": false,
  "balance": 0,
  "held_amount": 0,
  "available_balance": 0,
  "metadata": {},
  "created_at": "2026-11-02T00:23:23.640951Z"
}
```

## 2. Deposit $100.00: debit cash, credit Alice's wallet

```bash
api $BASE/transactions -H "Idempotency-Key: deposit-alice-001" -d @- <<JSON | jq
{"description": "Card top-up",
 "entries": [{"account_id": "$CASH", "amount": 10000, "currency": "USD"},
             {"account_id": "$ALICE", "amount": -10000, "currency": "USD"}]}
JSON
```

```json
{
  "id": "b69f6790-5285-43f1-8239-6cefc0bfe027",
  "description": "Card top-up",
  "metadata": {},
  "posted_at": "2026-11-02T00:23:23.761724Z",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "captures_hold_id": null,
  "entries": [
    {
      "account_id": "6595e26f-4ac1-4912-841a-ee52e2b7213a",
      "amount": 10000,
      "currency": "USD"
    },
    {
      "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
      "amount": -10000,
      "currency": "USD"
    }
  ]
}
```

## 3. Alice pays Bob $25.00

```bash
TRANSFER=$(cat <<JSON
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 2500, "currency": "USD", "description": "Dinner"}
JSON
)
api $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d "$TRANSFER" | jq
```

```json
{
  "id": "c1b68172-b8f8-4ee0-9366-d2c9d03807df",
  "description": "Dinner",
  "metadata": {},
  "posted_at": "2026-11-02T00:23:23.831301Z",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "captures_hold_id": null,
  "entries": [
    {
      "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
      "amount": 2500,
      "currency": "USD"
    },
    {
      "account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
      "amount": -2500,
      "currency": "USD"
    }
  ]
}
```

Both wallets are credit-normal, so the transfer debits Alice (`+2500`) and
credits Bob (`-2500`).

## 4. Retry with the same key: the original response is replayed

```bash
api -i $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d "$TRANSFER"
```

```text
HTTP/1.1 201 Created
date: Mon, 02 Nov 2026 00:23:23 GMT
server: uvicorn
content-length: 396
content-type: application/json
idempotent-replayed: true
x-request-id: 7d3a3f607b7547b7adb66cc81675c188

{"id":"c1b68172-b8f8-4ee0-9366-d2c9d03807df","description":"Dinner","metadata":{},"posted_at":"2026-11-02T00:23:23.831301Z","reverses_transaction_id":null,"reversed_by_transaction_id":null,"captures_hold_id":null,"entries":[{"account_id":"f5b5de30-e192-43a9-8eb7-d6016573a854","amount":2500,"currency":"USD"},{"account_id":"287e7e50-7843-45a0-b572-f1cf16f52811","amount":-2500,"currency":"USD"}]}
```

Same transaction ID, no second transfer. (uvicorn refreshes its `date` header
once a second, so it can trail the timestamps in the body.)

## 5. Reusing the key for a different request is rejected

```bash
api $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d @- <<JSON | jq
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 9900, "currency": "USD", "description": "Dinner"}
JSON
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#idempotency-key-reused",
  "title": "Idempotency key reused with a different request",
  "status": 409,
  "detail": "this Idempotency-Key was already used for a different request; use a new key for a new request",
  "request_id": "00cb90a69ddb435c9085590fd7a321e4"
}
```

## 6. Overdrafts are refused

```bash
api $BASE/transfers -H "Idempotency-Key: bob-pays-rent" -d @- <<JSON | jq
{"from_account_id": "$BOB", "to_account_id": "$ALICE", "amount": 100000, "currency": "USD"}
JSON
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#insufficient-funds",
  "title": "Insufficient funds",
  "status": 422,
  "detail": "account 287e7e50-7843-45a0-b572-f1cf16f52811 has a balance of 2500 and does not allow it to go below zero; the requested change was -100000",
  "account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
  "balance": 2500,
  "held_amount": 0,
  "available_balance": 2500,
  "change": -100000,
  "request_id": "c0ae827084864515b4f42302718495ce"
}
```

## 7. Balances and Alice's statement

```bash
api $BASE/accounts | jq -c '.data[] | {name, normal_balance, balance}'
api $BASE/accounts/$ALICE/entries | jq
```

```text
{"name":"Cash at bank","normal_balance":"debit","balance":10000}
{"name":"Alice wallet","normal_balance":"credit","balance":7500}
{"name":"Bob wallet","normal_balance":"credit","balance":2500}
```

```json
{
  "data": [
    {
      "transaction_id": "b69f6790-5285-43f1-8239-6cefc0bfe027",
      "posted_at": "2026-11-02T00:23:23.761724Z",
      "description": "Card top-up",
      "amount": -10000,
      "currency": "USD",
      "balance_after": 10000
    },
    {
      "transaction_id": "c1b68172-b8f8-4ee0-9366-d2c9d03807df",
      "posted_at": "2026-11-02T00:23:23.831301Z",
      "description": "Dinner",
      "amount": 2500,
      "currency": "USD",
      "balance_after": 7500
    }
  ],
  "next_cursor": null
}
```

## 8. Reverse the transfer, then look back in time

```bash
DINNER=$(api $BASE/accounts/$BOB/entries | jq -r '.data[0].transaction_id')
DINNER_AT=$(api $BASE/transactions/$DINNER | jq -r .posted_at)
api $BASE/transactions/$DINNER/reverse -H "Idempotency-Key: undo-dinner" -d '{"description": "Refund: duplicate charge"}' | jq
api $BASE/accounts | jq -c '.data[] | {name, balance}'
api "$BASE/accounts/$ALICE/balance?as_of=$DINNER_AT" | jq
```

```json
{
  "id": "5ff17c6c-606d-4d3a-a0e4-216339e45bfe",
  "description": "Refund: duplicate charge",
  "metadata": {},
  "posted_at": "2026-11-02T00:23:24.017228Z",
  "reverses_transaction_id": "c1b68172-b8f8-4ee0-9366-d2c9d03807df",
  "reversed_by_transaction_id": null,
  "captures_hold_id": null,
  "entries": [
    {
      "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
      "amount": -2500,
      "currency": "USD"
    },
    {
      "account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
      "amount": 2500,
      "currency": "USD"
    }
  ]
}
```

```text
{"name":"Cash at bank","balance":10000}
{"name":"Alice wallet","balance":10000}
{"name":"Bob wallet","balance":0}
```

```json
{
  "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "currency": "USD",
  "normal_balance": "credit",
  "balance": 7500,
  "held_amount": 0,
  "available_balance": 7500,
  "as_of": "2026-11-02T00:23:23.831301Z"
}
```

The original transfer is untouched; the reversal is a new transaction with
every entry negated, and the balance as of the transfer is still 7500.

## 9. A transaction can be reversed only once

```bash
api -X POST $BASE/transactions/$DINNER/reverse -H "Idempotency-Key: undo-dinner-again" | jq
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#already-reversed",
  "title": "Transaction already reversed",
  "status": 409,
  "detail": "transaction c1b68172-b8f8-4ee0-9366-d2c9d03807df was already reversed by 5ff17c6c-606d-4d3a-a0e4-216339e45bfe",
  "transaction_id": "c1b68172-b8f8-4ee0-9366-d2c9d03807df",
  "reversed_by_transaction_id": "5ff17c6c-606d-4d3a-a0e4-216339e45bfe",
  "request_id": "3e84efeb0140421bace3b5001ba7c306"
}
```

## 10. Authorize a hold: reserve $40.00 for a ride

A hold reserves funds on one account for a later transfer to another, the way
a card payment is authorized before it settles. Nothing is posted yet. The
hold follows the transfer rules, needs the amount to be available, and lapses
at `expires_at` if nobody captures or voids it first.

```bash
EXPIRES=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)
api $BASE/holds -H "Idempotency-Key: ride-hold-001" -d @- <<JSON | tee hold.json | jq
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 4000, "currency": "USD",
 "expires_at": "$EXPIRES", "description": "Ride to the airport"}
JSON
RIDE=$(jq -r .id hold.json)
```

```json
{
  "id": "4d98931a-2054-40b9-bc60-ff6fafb8a231",
  "status": "active",
  "from_account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "to_account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
  "amount": 4000,
  "captured_amount": null,
  "currency": "USD",
  "description": "Ride to the airport",
  "metadata": {},
  "created_at": "2026-11-02T00:23:24.163692Z",
  "expires_at": "2026-11-02T01:23:24Z",
  "closed_at": null,
  "capture_transaction_id": null
}
```

## 11. The ledger balance stays, the available balance drops

```bash
api $BASE/accounts/$ALICE | jq '{name, balance, held_amount, available_balance}'
```

```json
{
  "name": "Alice wallet",
  "balance": 10000,
  "held_amount": 4000,
  "available_balance": 6000
}
```

## 12. Held funds cannot be spent

A transfer of $65.00 fits the ledger balance but not the available balance:

```bash
api $BASE/transfers -H "Idempotency-Key: alice-buys-a-bike" -d @- <<JSON | jq
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 6500, "currency": "USD"}
JSON
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#insufficient-funds",
  "title": "Insufficient funds",
  "status": 422,
  "detail": "account f5b5de30-e192-43a9-8eb7-d6016573a854 has a balance of 10000, of which 4000 is reserved by active holds, so 6000 is available; it does not allow the available balance to go below zero and the requested change was -6500",
  "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "balance": 10000,
  "held_amount": 4000,
  "available_balance": 6000,
  "change": -6500,
  "request_id": "2936db7c8ac9428ca7331e9c838b99ea"
}
```

## 13. Capture $32.00 of the hold; the rest is released

The ride cost less than authorized. The capture posts a journal transaction
for the captured amount and releases the other $8.00:

```bash
api $BASE/holds/$RIDE/capture -H "Idempotency-Key: ride-capture-001" -d '{"amount": 3200}' | tee capture.json | jq
api $BASE/accounts | jq -c '.data[] | {name, balance, held_amount, available_balance}'
```

```json
{
  "id": "4d98931a-2054-40b9-bc60-ff6fafb8a231",
  "status": "captured",
  "from_account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "to_account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
  "amount": 4000,
  "captured_amount": 3200,
  "currency": "USD",
  "description": "Ride to the airport",
  "metadata": {},
  "created_at": "2026-11-02T00:23:24.163692Z",
  "expires_at": "2026-11-02T01:23:24Z",
  "closed_at": "2026-11-02T00:23:24.351727Z",
  "capture_transaction_id": "1485c763-5a6a-4add-9122-9588adcbb970"
}
```

```text
{"name":"Cash at bank","balance":10000,"held_amount":0,"available_balance":10000}
{"name":"Alice wallet","balance":6800,"held_amount":0,"available_balance":6800}
{"name":"Bob wallet","balance":3200,"held_amount":0,"available_balance":3200}
```

## 14. The capture is an ordinary journal transaction

`captures_hold_id` links it to the hold, and a database constraint keeps each
hold in the journal at most once. To refund it, reverse it like any other
transaction.

```bash
api $BASE/transactions/$(jq -r .capture_transaction_id capture.json) | jq
```

```json
{
  "id": "1485c763-5a6a-4add-9122-9588adcbb970",
  "description": "Capture of hold 4d98931a-2054-40b9-bc60-ff6fafb8a231",
  "metadata": {},
  "posted_at": "2026-11-02T00:23:24.351727Z",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "captures_hold_id": "4d98931a-2054-40b9-bc60-ff6fafb8a231",
  "entries": [
    {
      "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
      "amount": 3200,
      "currency": "USD"
    },
    {
      "account_id": "287e7e50-7843-45a0-b572-f1cf16f52811",
      "amount": -3200,
      "currency": "USD"
    }
  ]
}
```

## 15. Void a hold; a capture after the void is refused

```bash
TIP=$(api $BASE/holds -H "Idempotency-Key: tip-hold-001" -d @- <<JSON | jq -r .id
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 500, "currency": "USD", "expires_at": "$EXPIRES"}
JSON
)
api -X POST $BASE/holds/$TIP/void -H "Idempotency-Key: tip-void-001" | jq '{id, status, amount, closed_at}'
api $BASE/holds/$TIP/capture -H "Idempotency-Key: tip-capture-001" -d '{}' | jq
```

```json
{
  "id": "08c162a4-b869-4e4c-9d1d-bba9a60cff5a",
  "status": "voided",
  "amount": 500,
  "closed_at": "2026-11-02T00:23:24.497892Z"
}
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#hold-not-active",
  "title": "Hold is no longer active",
  "status": 409,
  "detail": "hold 08c162a4-b869-4e4c-9d1d-bba9a60cff5a is already voided; only an active hold can be captured or voided",
  "hold_id": "08c162a4-b869-4e4c-9d1d-bba9a60cff5a",
  "hold_status": "voided",
  "request_id": "7230313b36114071ba73a96d8d874f93"
}
```

The same `409 hold-not-active` answers whichever request loses a race between
a capture and a void (or two captures) of one hold.

## 16. An expired hold frees its funds and cannot be captured

```bash
SOON=$(date -u -d '+3 seconds' +%Y-%m-%dT%H:%M:%SZ)
SNACK=$(api $BASE/holds -H "Idempotency-Key: snack-hold-001" -d @- <<JSON | jq -r .id
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 300, "currency": "USD", "expires_at": "$SOON"}
JSON
)
api $BASE/accounts/$ALICE | jq -c '{balance, held_amount, available_balance}'
sleep 4
api $BASE/holds/$SNACK | jq -c '{status, expires_at}'
api $BASE/accounts/$ALICE | jq -c '{balance, held_amount, available_balance}'
api -X POST $BASE/holds/$SNACK/capture -H "Idempotency-Key: snack-capture-001" | jq
```

```text
{"balance":6800,"held_amount":300,"available_balance":6500}
{"status":"expired","expires_at":"2026-11-02T00:23:27Z"}
{"balance":6800,"held_amount":0,"available_balance":6800}
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#hold-expired",
  "title": "Hold expired",
  "status": 409,
  "detail": "hold ddca5019-caba-4cce-9b31-86ab9582febb expired at 2026-11-02T00:23:27Z; its funds are available again, so authorize a new hold to take them",
  "hold_id": "ddca5019-caba-4cce-9b31-86ab9582febb",
  "hold_status": "expired",
  "expires_at": "2026-11-02T00:23:27Z",
  "request_id": "2808e3facceb447f82dc7d94f89bf240"
}
```

Nothing ran when the hold expired. Expiry is lazy: from `expires_at` on, the
hold is reported as `expired` and no longer counts against the available
balance.

## 17. Available balances are point-in-time too

As of the moment the ride hold was authorized:

```bash
HELD_AT=$(jq -r .created_at hold.json)
api "$BASE/accounts/$ALICE/balance?as_of=$HELD_AT" | jq
```

```json
{
  "account_id": "f5b5de30-e192-43a9-8eb7-d6016573a854",
  "currency": "USD",
  "normal_balance": "credit",
  "balance": 10000,
  "held_amount": 4000,
  "available_balance": 6000,
  "as_of": "2026-11-02T00:23:24.163692Z"
}
```

## 18. Reconcile

```bash
uv run ledgerline reconcile
```

```text
accounts: 3  transactions: 4  entries: 8  holds: 3
[ok] stored balances equal the sum of their entries
[ok] every transaction sums to zero per currency
[ok] every entry uses its account's currency
[ok] trial balance is zero per currency (USD 0)
[ok] every hold's state, amounts and timestamps follow the hold state machine
[ok] active holds fit within the balance of every account that cannot go negative
[ok] every captured hold is in the journal exactly once, for the captured amount
Ledger is consistent.
```

The server logged one JSON line per request. This is the replay from step 4
(same `request_id` as its `X-Request-ID` header; `client` is a fingerprint of
the API key, never the key itself):

```json
{"time": "2026-11-02T00:23:23.848+00:00", "level": "INFO", "logger": "ledgerline.http", "message": "request completed", "request_id": "7d3a3f607b7547b7adb66cc81675c188", "method": "POST", "path": "/v1/transfers", "status": 201, "duration_ms": 1.96, "client": "57c8139db7290a90"}
```
