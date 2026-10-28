# API walkthrough

A complete session against the ledgerline API, request by request. Back to the
[README](../README.md).

Everything below was run against a local server (`uv run ledgerline serve` on
SQLite, freshly migrated); the IDs and timestamps are from that run. It uses
`curl` and `jq`.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/accounts`, `GET /v1/accounts`, `GET /v1/accounts/{id}` | Open, list (cursor-paginated) and read accounts |
| `GET /v1/accounts/{id}/balance?as_of=...` | Current or point-in-time balance |
| `GET /v1/accounts/{id}/entries` | Statement with running balances (cursor-paginated) |
| `POST /v1/transactions`, `GET /v1/transactions/{id}` | Post or read a journal transaction |
| `POST /v1/transfers` | Move money between two accounts with the same normal balance side |
| `POST /v1/transactions/{id}/reverse` | Reverse a transaction |
| `GET /healthz`, `GET /readyz` | Liveness and readiness (no authentication) |

**Sign convention.** Entry amounts are signed: positive is a debit, negative a
credit. Balances are reported in each account's own terms: assets grow with
debits, while liabilities such as a customer's wallet (money the platform owes
the customer) grow with credits.

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
  "id": "cb7349e6-9b9a-4a4b-bbc3-eb216375b018",
  "name": "Alice wallet",
  "type": "liability",
  "currency": "USD",
  "normal_balance": "credit",
  "allow_negative": false,
  "balance": 0,
  "metadata": {},
  "created_at": "2026-10-23T21:54:20.668408Z"
}
```

## 2. Deposit $100.00: debit cash, credit Alice's wallet

```bash
api $BASE/transactions -H "Idempotency-Key: deposit-alice-001" -d @- <<EOF | jq
{"description": "Card top-up",
 "entries": [{"account_id": "$CASH", "amount": 10000, "currency": "USD"},
             {"account_id": "$ALICE", "amount": -10000, "currency": "USD"}]}
EOF
```

```json
{
  "id": "cd1c8d95-1cb5-47de-a648-1b5c8104cafc",
  "description": "Card top-up",
  "metadata": {},
  "posted_at": "2026-10-23T21:54:20.704840Z",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "entries": [
    {
      "account_id": "5f163ec1-c56a-44c4-8ac5-ee8816c674a8",
      "amount": 10000,
      "currency": "USD"
    },
    {
      "account_id": "cb7349e6-9b9a-4a4b-bbc3-eb216375b018",
      "amount": -10000,
      "currency": "USD"
    }
  ]
}
```

## 3. Alice pays Bob $25.00

```bash
TRANSFER=$(cat <<EOF
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 2500, "currency": "USD", "description": "Dinner"}
EOF
)
api $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d "$TRANSFER" | jq
```

```json
{
  "id": "59d1063d-3736-4462-bff3-303e874da385",
  "description": "Dinner",
  "metadata": {},
  "posted_at": "2026-10-23T21:54:20.721402Z",
  "reverses_transaction_id": null,
  "reversed_by_transaction_id": null,
  "entries": [
    {
      "account_id": "cb7349e6-9b9a-4a4b-bbc3-eb216375b018",
      "amount": 2500,
      "currency": "USD"
    },
    {
      "account_id": "538b137e-b66e-403e-99e2-8f23682083c5",
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
date: Fri, 23 Oct 2026 21:54:19 GMT
server: uvicorn
content-length: 372
content-type: application/json
idempotent-replayed: true
x-request-id: 9e6119705dbc4ccea86a62ac23f6fce9

{"id":"59d1063d-3736-4462-bff3-303e874da385","description":"Dinner","metadata":{},"posted_at":"2026-10-23T21:54:20.721402Z","reverses_transaction_id":null,"reversed_by_transaction_id":null,"entries":[{"account_id":"cb7349e6-9b9a-4a4b-bbc3-eb216375b018","amount":2500,"currency":"USD"},{"account_id":"538b137e-b66e-403e-99e2-8f23682083c5","amount":-2500,"currency":"USD"}]}
```

Same transaction ID, no second transfer. (uvicorn refreshes its `date` header
once a second, so it can trail the timestamps in the body.)

## 5. Reusing the key for a different request is rejected

```bash
api $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d @- <<EOF | jq
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 9900, "currency": "USD", "description": "Dinner"}
EOF
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#idempotency-key-reused",
  "title": "Idempotency key reused with a different request",
  "status": 409,
  "detail": "this Idempotency-Key was already used for a different request; use a new key for a new request",
  "request_id": "4115b3b2a64340b881c94509e39e67fc"
}
```

## 6. Overdrafts are refused

```bash
api $BASE/transfers -H "Idempotency-Key: bob-pays-rent" -d @- <<EOF | jq
{"from_account_id": "$BOB", "to_account_id": "$ALICE", "amount": 100000, "currency": "USD"}
EOF
```

```json
{
  "type": "https://github.com/ult666666/ledgerline/blob/main/docs/errors.md#insufficient-funds",
  "title": "Insufficient funds",
  "status": 422,
  "detail": "account 538b137e-b66e-403e-99e2-8f23682083c5 has a balance of 2500 and does not allow it to go below zero; the requested change was -100000",
  "account_id": "538b137e-b66e-403e-99e2-8f23682083c5",
  "balance": 2500,
  "change": -100000,
  "request_id": "64a03f8983dc4a3c87baf30e8c60cf44"
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
      "transaction_id": "cd1c8d95-1cb5-47de-a648-1b5c8104cafc",
      "posted_at": "2026-10-23T21:54:20.704840Z",
      "description": "Card top-up",
      "amount": -10000,
      "currency": "USD",
      "balance_after": 10000
    },
    {
      "transaction_id": "59d1063d-3736-4462-bff3-303e874da385",
      "posted_at": "2026-10-23T21:54:20.721402Z",
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
  "id": "f9bec210-a904-4230-9c68-098a0ec160ca",
  "description": "Refund: duplicate charge",
  "metadata": {},
  "posted_at": "2026-10-23T21:54:20.815405Z",
  "reverses_transaction_id": "59d1063d-3736-4462-bff3-303e874da385",
  "reversed_by_transaction_id": null,
  "entries": [
    {
      "account_id": "cb7349e6-9b9a-4a4b-bbc3-eb216375b018",
      "amount": -2500,
      "currency": "USD"
    },
    {
      "account_id": "538b137e-b66e-403e-99e2-8f23682083c5",
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
  "account_id": "cb7349e6-9b9a-4a4b-bbc3-eb216375b018",
  "currency": "USD",
  "normal_balance": "credit",
  "balance": 7500,
  "as_of": "2026-10-23T21:54:20.721402Z"
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
  "detail": "transaction 59d1063d-3736-4462-bff3-303e874da385 was already reversed by f9bec210-a904-4230-9c68-098a0ec160ca",
  "transaction_id": "59d1063d-3736-4462-bff3-303e874da385",
  "reversed_by_transaction_id": "f9bec210-a904-4230-9c68-098a0ec160ca",
  "request_id": "ca5f44ef693b49459d53eb5db352f57e"
}
```

## 10. Reconcile

```bash
uv run ledgerline reconcile
```

```text
accounts: 3  transactions: 3  entries: 6
[ok] stored balances equal the sum of their entries
[ok] every transaction sums to zero per currency
[ok] every entry uses its account's currency
[ok] trial balance is zero per currency (USD 0)
Ledger is consistent.
```

The server logged one JSON line per request. This is the replay from step 4
(same `request_id` as its `X-Request-ID` header; `client` is a fingerprint of
the API key, never the key itself):

```json
{"time": "2026-10-23T21:54:20.734+00:00", "level": "INFO", "logger": "ledgerline.http", "message": "request completed", "request_id": "9e6119705dbc4ccea86a62ac23f6fce9", "method": "POST", "path": "/v1/transfers", "status": 201, "duration_ms": 1.51, "client": "57c8139db7290a90"}
```
