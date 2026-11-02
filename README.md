# ledgerline

A double-entry ledger as an HTTP service. Journal entries always balance and are never
edited, money-moving requests are idempotent, balance checks hold under concurrent
writes, and holds reserve funds for a later capture, the way a card authorization does.
Built with FastAPI and SQLAlchemy 2 on PostgreSQL or SQLite.

[![CI](https://github.com/ult666666/ledgerline/actions/workflows/ci.yml/badge.svg)](https://github.com/ult666666/ledgerline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12 | 3.13](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](pyproject.toml)

## Run it

With [uv](https://docs.astral.sh/uv/) installed:

```bash
git clone https://github.com/ult666666/ledgerline.git
cd ledgerline
uv sync
cp .env.example .env          # SQLite file + a placeholder API key
uv run alembic upgrade head   # create the schema
uv run ledgerline serve       # http://127.0.0.1:8000, interactive docs at /docs
```

The placeholder key, `dev-key-change-me`, is for local use only. Generate a real one
with `uv run ledgerline generate-key`.

To see the main behaviours without starting anything yourself, run
`uv run python examples/wallet_demo.py`. It runs a deposit, a transfer, a retried
request, a refused overdraft and a reversal against a temporary server and database,
prints a JSON summary of each check, and cleans up ([details](docs/WALLET_DEMO.md)).
`docker compose up --build` starts the API next to PostgreSQL 17 instead; it applies
the migrations first and uses the same placeholder key unless `LEDGERLINE_API_KEYS` is
set.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./ledgerline.db` | SQLAlchemy URL. `postgres://` and `postgresql://` URLs are switched to the psycopg 3 driver. |
| `LEDGERLINE_API_KEYS` | (empty) | Comma-separated bearer tokens. With none configured every `/v1` request gets a `401`. |
| `LEDGERLINE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |

## A tour of the API

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

Entry amounts are signed integers in minor units (cents for USD): positive is a debit,
negative a credit. Balances are reported in each account's own terms, so an asset grows
with debits, while a liability such as a customer's wallet (money the platform owes the
customer) grows with credits.

A deposit and a transfer with `curl` and `jq`, against the local server:

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

A hold reserves money now and moves it later. Alice reserves $40.00 for a ride, then
the ride costs $32.00:

```bash
EXPIRES=$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)   # GNU date
RIDE=$(api $BASE/holds -H "Idempotency-Key: ride-hold-001" -d @- <<EOF | jq -r .id
{"from_account_id": "$ALICE", "to_account_id": "$BOB", "amount": 4000,
 "currency": "USD", "expires_at": "$EXPIRES"}
EOF
)
api $BASE/accounts/$ALICE | jq '{balance, held_amount, available_balance}'
api $BASE/holds/$RIDE/capture -H "Idempotency-Key: ride-capture-001" -d '{"amount": 3200}' | jq .status
```

The hold leaves Alice's ledger balance at 7500 and lowers her available balance to
3500. The capture posts 3200 as one journal transaction and releases the other 800.
[docs/API_WALKTHROUGH.md](docs/API_WALKTHROUGH.md) runs a longer session with every
response, including a rejected key reuse, a refused overdraft, statements, a reversal
with point-in-time balances, voided and expired holds, and reconciliation.
Every error is an RFC 9457 problem document; [docs/errors.md](docs/errors.md) lists them.

## Design notes

Systems that move money tend to fail in a few known ways. This is how ledgerline
handles each:

| Failure | What ledgerline does |
| --- | --- |
| A client times out, retries, and the payment happens twice | `Idempotency-Key` is required on every money-moving `POST`. A retry with the same body replays the stored response; the same key with a different body gets a `409`. |
| Two requests read the same balance and together overdraw it | Each account row carries a version. Writes compare-and-swap it in the same database transaction as the entries, and retry on conflict. |
| Floating point loses cents (`0.1 + 0.2 != 0.3`) | Amounts are JSON integers in minor units. Floats and numeric strings are rejected. |
| Editing a posted record makes balances impossible to audit | There are no update or delete endpoints. A correction is a reversal transaction, at most one per transaction. |
| Money reserved by a hold gets spent twice | Holds and postings both draw on the available balance (ledger minus held) under the same compare-and-swap. |

A write runs in one database transaction: look up the idempotency key, load the
accounts, apply the posting rules, compare-and-swap each balance
(`UPDATE ... WHERE id = ? AND version = ?`) in a fixed order, insert the journal rows,
store the response under the key, and commit. If an account changed underneath, or a
concurrent duplicate claimed the key, everything rolls back and the request is retried
with fresh data, up to 10 attempts with jittered backoff. `ledgerline reconcile`
recomputes the invariants from the journal.

| Module | Responsibility |
| --- | --- |
| `api/` | Routes, bearer-key auth, Idempotency-Key handling, RFC 9457 error mapping, request middleware |
| `service.py` | Transaction boundaries: idempotency lookup, posting, compare-and-swap, retries |
| `domain.py` | Pure double-entry rules (balancing, normal balances, overdrafts, transfers, reversals) and the hold state machine |
| `repository.py` | Every SQL statement, including the version-checked balance update |
| `models.py`, `migrations/` | Typed SQLAlchemy models and the Alembic migrations, with CHECK, UNIQUE and FK constraints |
| `reconcile.py`, `cli.py` | Invariant checks over the journal; `ledgerline serve`, `reconcile` and `generate-key` |
| `observability.py` | JSON log formatter and the request-ID context variable |

[docs/DESIGN.md](docs/DESIGN.md) explains each choice and its cost: why optimistic
versioning instead of `SELECT ... FOR UPDATE`, why only successful responses are stored
under a key, why hold expiry is computed on read, and why API keys are hashed with
SHA-256. It also has the list of invariants, the testing strategy and the references.

## Tests

A Hypothesis state machine drives the API with random sequences of transfers,
multi-currency transactions, reversals, replays and key reuse, and checks the database
against a simple model after every step. A second state machine does the same for
holds, with a fake clock that expires them. Threaded tests race transfers, holds and
captures against one account and check that exactly the affordable set succeeds.
Migration tests upgrade an empty database and one that already holds data.

```bash
uv sync                                   # runtime + dev dependencies
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict mode
uv run pytest --cov                       # SQLite; fails below 90% coverage
```

The suite runs against PostgreSQL when `DATABASE_URL` is set. Every test deletes all
rows, so the database name must contain `test`:

```bash
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/ledgerline_test uv run pytest
```

Without Docker, `uv run --with pgserver python scripts/pytest_on_pgserver.py` runs the
suite against a throwaway PostgreSQL from the [pgserver](https://pypi.org/project/pgserver/)
package. CI runs the suite on Python 3.12 and 3.13 with SQLite and on PostgreSQL 17,
builds and smoke-tests the Docker image, and drives the compose stack end to end with
`scripts/compose_smoke.py`.

## Limits

- Authentication is a shared list of API keys with identical permissions, with no
  per-client authorization, key rotation or rate limiting.
- Each account holds one currency, and the service does no FX conversion.
- Optimistic concurrency retries instead of queueing, so many simultaneous writes to
  one account can end in `409 concurrent-update`.
- A hold allows one capture and cannot be raised, and its expiry is computed on read
  rather than by a job, so nothing fires when it lapses.
- SQLite serialises writers and suits development; use PostgreSQL for concurrent load.

[docs/DESIGN.md](docs/DESIGN.md#14-out-of-scope) lists what is out of scope in more
detail.

Built with AI coding assistants. The tests and CI checks described above verify its
behaviour.

## License

Released under the [MIT License](LICENSE).
