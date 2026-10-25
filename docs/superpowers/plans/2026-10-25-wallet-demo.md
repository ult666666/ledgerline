# Wallet Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the wallet lifecycle and its accounting/retry invariants runnable in one disposable command.

**Architecture:** An example context manager owns a temporary SQLite database and spawned uvicorn process on a parent-bound ephemeral loopback socket. The child migrates with explicit settings; the parent exercises HTTP and reconciles locally, then prints a verified JSON receipt after cleanup.

**Tech Stack:** Python 3.12+, stdlib multiprocessing/socket/http.client, existing FastAPI/uvicorn/Alembic/SQLAlchemy APIs; no added dependencies.

**Spec:** `docs/superpowers/specs/2026-10-25-wallet-demo-design.md`

## Global Constraints

- Explicit disposable SQLite settings; ignore caller DATABASE_URL and .env.
- Real HTTP only on 127.0.0.1 and an owned ephemeral listener; no caller-supplied URL/port/database.
- Startup and requests share a default 15-second deadline; shutdown has bounded terminate/kill joins.
- Deposit 10000 cents, transfer 2500, replay same transaction, reject overdraft without changes, reverse, inspect historical/current balances, reconcile trial balance zero.
- Print actual IDs, balances and checked invariants; never print the random API key.

## Review Focus

- Startup timeout must stop the child and delete its database.
- A workflow exception must still release the process, listener and directory.
- Existing caller database bytes and unrelated listeners must remain unchanged.
- Replay and rejected requests must not create extra journal entries.
- Historical balances after reversal must retain the original transfer state.

### Task 1: Owned server and lifecycle receipt

**Files:** Create `examples/wallet_demo.py`, `tests/test_wallet_demo.py`, `docs/WALLET_DEMO.md`; modify README and add examples to existing mypy coverage.

**Interfaces:** `DemoServer(timeout: float = 15)` context manager with `request(method, path, body=None, key=None) -> Reply`; `exercise_wallet(server) -> dict[str, Any]`; `run_demo(timeout=15) -> dict[str, Any]`; `main(argv=None) -> int`.

- [ ] Write failing real-HTTP tests for receipt invariants, caller environment isolation, unrelated listener ownership, context-error cleanup and startup-timeout cleanup.
- [ ] Run `uv run pytest tests/test_wallet_demo.py -q`; expect failure because the example is absent.
- [ ] Implement minimal owned-process lifecycle and verified wallet workflow; run focused tests.
- [ ] Add one-command README walkthrough, output contract and cleanup/timeout limits.
- [ ] Run full existing suite with coverage, ruff/format/mypy and actual documented CLI. Expect 160 existing tests plus new tests green.
- [ ] Fetch/preserve concurrent main changes, commit and push scoped PR, attach it; no merge, parent reviews exact head.
