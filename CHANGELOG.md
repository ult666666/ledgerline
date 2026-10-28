# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `examples/wallet_demo.py`: a wallet lifecycle (deposit, transfer, retried
  request, refused overdraft, reversal, historical balances, reconciliation)
  against a temporary local server and database. The walkthrough is in
  `docs/WALLET_DEMO.md`.
- `ledgerline generate-key`, which prints a bearer token made from 32 random
  bytes.
- A startup warning when a configured API key is shorter than 32 characters.
  The warning reports a count and never the keys.
- Monthly Dependabot updates for uv dependencies and GitHub Actions.

### Changed

- The build backend requirement is now `uv_build>=0.13.5,<0.14.0`.

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
