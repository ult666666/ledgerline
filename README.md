# ledgerline

A double-entry ledger service: balanced, immutable journal entries, idempotent
writes and race-free balance checks, built with FastAPI, SQLAlchemy 2 and
PostgreSQL or SQLite.

[![CI](https://github.com/ult666666/ledgerline/actions/workflows/ci.yml/badge.svg)](https://github.com/ult666666/ledgerline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](pyproject.toml)

## Why

Systems that move money tend to fail in the same few ways:

- **Double posting.** A client times out, retries, and the payment happens twice.
- **Races.** Two requests read the same balance, both decide there is enough
  money, and together they overdraw the account (a lost update).
- **Floats.** `0.1 + 0.2 != 0.3`, so binary floating point quietly loses cents.
- **Edited history.** Updating or deleting a posted record makes balances
  impossible to audit.

ledgerline is a small service that handles each of these deliberately and
tests each one. The reasoning behind every choice is in
[docs/DESIGN.md](docs/DESIGN.md).

## Features

- **Double-entry bookkeeping.** Accounts have a type (asset, liability,
  equity, revenue, expense), an ISO 4217 currency and a normal balance side.
  Journal transactions need two or more entries whose signed amounts sum to
  zero per currency.
- **Integer minor units only.** Amounts are JSON integers (cents for USD);
  floats and numeric strings are rejected.
- **Immutable journal.** No update or delete endpoints. Corrections are
  reversal transactions, at most one per transaction.
- **Overdraft protection under concurrency.** Each account stores its balance
  with a version column, updated with a compare-and-swap in the same database
  transaction as the entries, with bounded retries. Accounts with
  `allow_negative: false` cannot go below zero, even under concurrent requests.
- **Idempotent writes.** `Idempotency-Key` is required on money-moving `POST`s.
  A retry with the same body replays the original response
  (`Idempotent-Replayed: true`); the same key with a different body is a `409`;
  concurrent duplicates take effect exactly once.
- **Point-in-time balances and statements** with running balances and cursor
  pagination.
- **RFC 9457 errors** (`application/problem+json`) with a documented
  [error catalogue](docs/errors.md).
- **Operations basics:** structured JSON logs with request IDs and timings,
  `/healthz` and `/readyz`, bearer API keys compared in constant time, a
  `ledgerline reconcile` command, Alembic migrations, a Docker image that CI
  smoke-tests, and a docker-compose file with PostgreSQL.

## Quick start

Requires [uv](https://docs.astral.sh/uv/). Clone the repository and install its
dependencies:

```bash
git clone https://github.com/ult666666/ledgerline.git
cd ledgerline
uv sync
```

### Wallet demo

Run a deposit, a transfer, a retried request, a refused overdraft and a
reversal against a temporary local server and database. The demo cleans up
after itself:

```bash
uv run python examples/wallet_demo.py
```

It prints a JSON receipt with the result of every check, the historical
balances and a final reconciliation. The [walkthrough](docs/WALLET_DEMO.md)
explains the integer-cent balances, the temporary server, deadlines and
receipt fields.

### Run the API locally with SQLite

```bash
cp .env.example .env          # SQLite file + a placeholder API key
uv run alembic upgrade head   # create the schema
uv run ledgerline serve       # http://127.0.0.1:8000, interactive docs at /docs
```

The placeholder key is for local use only. For anything else, generate a key
with `uv run ledgerline generate-key` (see [Security notes](#security-notes)).

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./ledgerline.db` | SQLAlchemy URL. `postgres://` and `postgresql://` URLs are switched to the psycopg 3 driver. |
| `LEDGERLINE_API_KEYS` | (empty) | Comma-separated bearer tokens. With none configured every `/v1` request gets a `401`. |
| `LEDGERLINE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |

## API walkthrough

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

### 1. Open accounts

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

### 2. Deposit $100.00: debit cash, credit Alice's wallet

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

### 3. Alice pays Bob $25.00

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

### 4. Retry with the same key: the original response is replayed

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

### 5. Reusing the key for a different request is rejected

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

### 6. Overdrafts are refused

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

### 7. Balances and Alice's statement

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

### 8. Reverse the transfer, then look back in time

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

### 9. A transaction can be reversed only once

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

### 10. Reconcile

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

## Architecture

```mermaid
flowchart LR
    client([HTTP client]) --> mw["Middleware<br/>request ID, timing, access log"]
    mw --> api["FastAPI routes /v1<br/>auth, validation, problem+json"]
    api --> svc["LedgerService<br/>unit of work, idempotency, retries"]
    svc --> domain["domain.py<br/>pure posting rules"]
    svc --> repo["LedgerRepository<br/>all SQL"]
    repo --> db[("SQLite or PostgreSQL")]
    cli(["ledgerline reconcile"]) --> rec["reconcile.py"] --> db
```

| Module | Responsibility |
| --- | --- |
| `api/` | Routes, bearer-key auth, Idempotency-Key handling, RFC 9457 error mapping, request middleware |
| `service.py` | Transaction boundaries: idempotency lookup, posting, compare-and-swap, retries |
| `domain.py` | Pure double-entry rules (balancing, normal balances, overdrafts, transfers, reversals) |
| `repository.py` | Every SQL statement, including the version-checked balance update |
| `models.py`, `migrations/` | Typed SQLAlchemy models (`Mapped[...]`) and the Alembic migration, with CHECK, UNIQUE and FK constraints |
| `reconcile.py`, `cli.py` | Recompute the invariants from the journal; `ledgerline serve`, `ledgerline reconcile` and `ledgerline generate-key` |
| `observability.py` | JSON log formatter and the request-ID context variable |

A write runs in one database transaction: look up the idempotency key, load
the accounts, apply the posting rules, compare-and-swap each balance
(`UPDATE ... WHERE id = ? AND version = ?`) in a fixed order, insert the
journal rows, store the response under the key, commit. If an account changed
underneath (or a concurrent duplicate claimed the key), everything rolls back
and the request is retried with fresh data, up to 10 attempts in total with
jittered backoff. [docs/DESIGN.md](docs/DESIGN.md) explains why optimistic versioning
was chosen over `SELECT ... FOR UPDATE`, and what that costs.

## Correctness

| Invariant | Enforced by | Checked by |
| --- | --- | --- |
| Every transaction has two or more non-zero entries on distinct accounts and sums to zero per currency | `domain.validate_entries`; `CHECK (amount <> 0)`, `UNIQUE (transaction_id, account_id)` | Unit tests, API tests, state machine, `reconcile` |
| The trial balance is zero for every currency | Follows from the row above | State machine, `reconcile` |
| A stored balance equals the sum of its account's entries | Balance and entries change in one DB transaction, guarded by the version check | State machine, threaded tests, `reconcile` |
| An account with `allow_negative: false` never goes below zero | `domain.apply_entries` + compare-and-swap; `CHECK (allow_negative OR balance >= 0)` as a backstop | Threaded overdraft test, state machine |
| Posted transactions never change; each has at most one reversal, which cannot itself be reversed | No update or delete paths; `UNIQUE (reverses_id)` | API tests, state machine |
| One idempotency key, one effect | Response stored under `PRIMARY KEY (principal, key)` in the same DB transaction as the effect | Threaded duplicate test, forced key-race test, state machine replays |
| Statement running balances match the entries | `balance_after` written with each entry | State machine |

The test suite (`tests/`) includes:

- **Unit tests** for the domain rules, plus a Hypothesis property: any balanced
  posting validates and its reversal cancels it exactly.
- **API tests** through FastAPI's `TestClient` for every endpoint and error type.
- **A Hypothesis state machine** (`tests/test_stateful.py`) that drives the API
  with random sequences of account opening, transfers, multi-leg and
  multi-currency transactions, reversals, replays and key reuse. A simple model
  predicts each outcome, and after every step the test checks the database
  against the model, runs `reconcile` and re-derives every running balance.
- **Concurrency tests** (`tests/test_concurrency.py`) that release 40 transfers
  from one account at the same moment and check for overdrafts and lost
  updates, and send 10 identical requests sharing one key and check that
  exactly one takes effect. Further tests force the stale-read and key-race
  paths deterministically.
- **Migration tests** that run `alembic upgrade head` on an empty database,
  compare the result with the models and run the API on it.

## Development

```bash
uv sync                                   # runtime + dev dependencies
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict mode
uv run pytest --cov                       # SQLite; fails below 90% coverage
```

The suite runs against PostgreSQL when `DATABASE_URL` is set. Every test
deletes all rows, so the database name must contain `test`:

```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/ledgerline_test uv run pytest
```

Without Docker, `scripts/pytest_on_pgserver.py` applies the migrations and runs
the suite against a throwaway PostgreSQL from the
[pgserver](https://pypi.org/project/pgserver/) package (not a project
dependency):

```bash
uv run --with pgserver python scripts/pytest_on_pgserver.py
```

CI (`.github/workflows/ci.yml`) runs lint, types and tests on Python 3.12 and
3.13 with SQLite, the migrations and suite against a PostgreSQL 17 service
container, and a Docker build with a smoke test.

## Docker Compose with PostgreSQL (not yet run end to end)

`docker-compose.yml` starts the API next to a PostgreSQL 17 database. CI checks
that the file is valid and smoke-tests the API image on its own, but the full
compose stack has not been run end to end yet, so treat it as a starting point:

```bash
docker compose up --build     # API on http://127.0.0.1:8000, PostgreSQL 17 alongside
```

The API container applies migrations before it starts. The API key defaults to
`dev-key-change-me`, which is for local use only; set `LEDGERLINE_API_KEYS` to
override it.

## Security notes

- **Use generated keys.** `uv run ledgerline generate-key` prints a token made
  from 32 random bytes. Keep it in a protected environment or secret manager,
  never commit it or paste it into issues or logs, and send it only over HTTPS.
  The `dev-key-change-me` placeholder is for local demos only; the service logs
  a warning at startup if any configured key is shorter than 32 characters.
- **Why SHA-256 and not a password hash.** The service hashes bearer keys with
  SHA-256 for the constant-time comparison, and logs and idempotency records
  name each client by the first 16 hex digits of that hash. This is the
  standard pattern for random tokens: nobody can recover a 256-bit token from
  its hash, and a slow password hash would only add work to every request. It
  does not protect a weak, hand-typed key, which someone with the logs could
  guess offline. [docs/DESIGN.md](docs/DESIGN.md#11-errors-logging-and-authentication)
  has the details.

## Limitations and roadmap

This is a portfolio project, not production software. In particular:

- **Authentication is a shared list of API keys** with identical permissions.
  A real deployment needs proper identity (OAuth 2.0/OIDC or mTLS),
  per-client authorization, key rotation, rate limiting and audit logging.
- **No FX conversion.** Each account holds one currency; multi-currency
  transactions must balance per currency (for example through conversion
  accounts). Currency codes are format-checked, not validated against the
  ISO 4217 list.
- **Hot accounts.** Optimistic concurrency retries instead of queueing, so many
  simultaneous writes to one account cost retries and can end in a `409
  concurrent-update`. Row locks or a per-account queue suit that load better
  (see DESIGN.md).
- **SQLite is for development.** It serialises writers (`BEGIN IMMEDIATE`);
  use PostgreSQL for anything concurrent.
- **No pending or held funds and no backdated entries.** `posted_at` is
  assigned by the server, and point-in-time history assumes a single server
  clock.
- **Idempotency records are kept forever** and only successful responses are
  stored, so a request that failed can be retried with the same key.
- **No account updates or closures and no transaction search;** no
  multi-tenancy, sharding, event streaming (outbox) or metrics and tracing
  export.
- **Verification scope:** hosted CI runs the suite on SQLite (Python 3.12 and
  3.13) and on PostgreSQL 17, validates `docker-compose.yml`, builds the image
  and smoke-tests the running container (health check, `/healthz`, `/readyz`
  and an authenticated API call). It does not start the compose stack.

## References

- [RFC 9457: Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457)
- [The Idempotency-Key HTTP Header Field (IETF draft)](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)
- [Stripe API: idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [PostgreSQL: transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html),
  including how `UPDATE ... WHERE` behaves under concurrent updates at Read Committed
- [SQLite: BEGIN IMMEDIATE](https://www.sqlite.org/lang_transaction.html)
- [Exponential Backoff and Jitter (AWS Architecture Blog)](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 7
  (transactions, lost updates and write skew)

## License

Released under the [MIT License](LICENSE).
