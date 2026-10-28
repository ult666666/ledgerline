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
  `ledgerline reconcile` command, Alembic migrations, a Docker image, and a
  docker-compose stack with PostgreSQL that CI runs end to end.

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

## API

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

A deposit and a transfer with `curl` and `jq`, against the local server from
the quick start:

```bash
api() { curl -s -H "Authorization: Bearer dev-key-change-me" -H "Content-Type: application/json" "$@"; }
BASE=http://127.0.0.1:8000/v1
CASH=$(api $BASE/accounts -d '{"name": "Cash at bank", "type": "asset", "currency": "USD"}' | jq -r .id)
ALICE=$(api $BASE/accounts -d '{"name": "Alice wallet", "type": "liability", "currency": "USD"}' | jq -r .id)
BOB=$(api $BASE/accounts -d '{"name": "Bob wallet", "type": "liability", "currency": "USD"}' | jq -r .id)

# Deposit $100.00: debit cash, credit Alice's wallet
api $BASE/transactions -H "Idempotency-Key: deposit-alice-001" -d @- <<EOF | jq
{"description": "Card top-up",
 "entries": [{"account_id": "$CASH", "amount": 10000, "currency": "USD"},
             {"account_id": "$ALICE", "amount": -10000, "currency": "USD"}]}
EOF

# Alice pays Bob $25.00. Sending the same request again replays the first
# response (Idempotent-Replayed: true) instead of paying twice.
api $BASE/transfers -H "Idempotency-Key: dinner-alice-bob" -d @- <<EOF | jq
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 2500, "currency": "USD"}
EOF
```

[docs/API_WALKTHROUGH.md](docs/API_WALKTHROUGH.md) runs the full session with every
response: the replayed retry, a rejected key reuse, a refused overdraft,
statements, a reversal with point-in-time balances and reconciliation.

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
container, a Docker build with a smoke test, and the full compose stack driven
end to end by `scripts/compose_smoke.py`.

## Docker Compose with PostgreSQL

`docker-compose.yml` starts the API next to a PostgreSQL 17 database. CI runs
the whole stack on every push: it starts both containers, drives the API with
`scripts/compose_smoke.py`, then runs `ledgerline reconcile` inside the API
container. The script opens accounts, posts a deposit and a transfer, checks a
replayed retry, a rejected key reuse and a refused overdraft, and sends 20
concurrent transfers that together ask for more than the wallet holds: exactly
the 15 that fit succeed, and the balances still add up.

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
  and an authenticated API call). It also runs the compose stack (API and
  PostgreSQL 17) end to end, including 20 concurrent transfers against one
  wallet, and reconciles the result.

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
