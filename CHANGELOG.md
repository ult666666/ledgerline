# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Holds (two-phase transfers): `POST /v1/holds` authorizes an amount from one
  account toward another, `POST /v1/holds/{id}/capture` posts all or part of
  it as one journal transaction and releases the rest,
  `POST /v1/holds/{id}/void` releases it, and `GET /v1/holds/{id}` reads it.
  Holds expire lazily at their required `expires_at`. Every hold write uses
  `Idempotency-Key`.
- `held_amount` and `available_balance` (ledger balance minus active holds)
  on account and balance responses, including `as_of` balances.
  `captures_hold_id` on transactions.
- Problem types `invalid-hold`, `hold-not-active` and `hold-expired`.
- `ledgerline reconcile` checks that hold states follow the state machine,
  that active holds fit the balance of accounts that cannot go negative, and
  that each capture is in the journal exactly once, for the captured amount.
- Alembic revision 0002 (`holds` table, `transactions.hold_id`).
- Tests: a Hypothesis state machine of holds against an in-memory model,
  hold API, concurrency, constraint and migration tests. The compose smoke
  test also races 25 holds against one wallet.
- `LedgerService` and `create_app` accept a `clock`, which supplies every
  timestamp and expiry decision.

- `examples/wallet_demo.py`: a wallet lifecycle (deposit, transfer, retried
  request, refused overdraft, reversal, historical balances, reconciliation)
  against a temporary local server and database. The walkthrough is in
  `docs/WALLET_DEMO.md`.
- `ledgerline generate-key`, which prints a bearer token made from 32 random
  bytes.
- A startup warning when a configured API key is shorter than 32 characters.
  The warning reports a count and never the keys.
- Monthly Dependabot updates for uv dependencies and GitHub Actions.
- `scripts/compose_smoke.py` and a CI job that runs the Docker Compose stack
  (API and PostgreSQL 17) end to end: accounts, a deposit, a transfer, a
  replayed retry, a rejected key reuse, a refused overdraft and 20 concurrent
  transfers against one wallet, followed by `ledgerline reconcile`.

### Changed

- Postings (transfers, journal transactions, reversals) check the available
  balance, so they cannot spend funds reserved by holds. The
  `insufficient-funds` problem reports `held_amount` and `available_balance`.
- `ledgerline reconcile` prints the number of holds and three more checks.
- On SQLite, migrations turn foreign-key enforcement off while they run and
  check every reference before committing, so table rebuilds work on a
  database that already holds data.
- The build backend requirement is now `uv_build>=0.13.5,<0.14.0`.
- The full API walkthrough moved from the README to `docs/API_WALKTHROUGH.md`;
  the README keeps the endpoint table and a short example.

### Fixed

- On PostgreSQL, a reversal racing another reversal of the same transaction
  could fail with a 500 (`UNIQUE (reverses_id)`) instead of
  `409 already-reversed`. The existing-reversal check now runs after the
  balance read.
- A problem extension can no longer overwrite a standard RFC 9457 member such
  as `status`.
- Holds on an account that may go negative were not range-checked:
  `held_amount` and `available_balance` could leave +/-(2^53 - 1), and on
  SQLite about 1,025 maximal holds overflowed `SUM`, so every read or write of
  that account failed with a 500. Holds and postings that would take either
  balance out of range are now refused with a 422.
- A hold `expires_at` or a balance `as_of` with an offset that moves it outside
  the years 1 to 9999 in UTC (for example `9999-12-31T23:00:00-05:00`) caused a
  500; it is now a `422 validation-error`.

## 0.1.0 - 2026-10-23

### Added

- Double-entry domain: accounts with type, ISO 4217 currency code, normal balance,
  `allow_negative` flag and metadata; journal transactions of two or more
  signed entries in integer minor units that balance per currency.
- Immutable journal with reversal transactions (at most one per transaction).
- Versioned REST API under `/v1`: accounts with cursor pagination, current and
  point-in-time balances, statements with running balances, journal
  transactions, transfers and reversals.
- `Idempotency-Key` support: replays with `Idempotent-Replayed: true`, `409`
  on key reuse with a different request, exactly-once effect for concurrent
  duplicates.
- Optimistic concurrency control on account balances (version column,
  compare-and-swap, bounded retries with jittered backoff).
- RFC 9457 problem responses and an error catalogue (`docs/errors.md`).
- Bearer API-key authentication with constant-time comparison.
- Structured JSON logging with request IDs and timings; `/healthz` and `/readyz`.
- `ledgerline serve` and `ledgerline reconcile` commands.
- Alembic migration for SQLite and PostgreSQL.
- Test suite: unit, API, Hypothesis state machine, concurrency and migration
  tests; runs on SQLite or PostgreSQL.
- Multi-stage Dockerfile, docker-compose stack with PostgreSQL 17, and a
  GitHub Actions workflow.
