# Run a disposable wallet lifecycle

From a source checkout after `uv sync`, run:

```bash
uv run python examples/wallet_demo.py
```

The example starts a real local HTTP service, exercises the wallet lifecycle,
checks its accounting and retry invariants, then prints a JSON receipt after
cleanup. It uses the project's existing Alembic migration, application factory
and reconciliation API. All accounts and amounts belong to this disposable
example; no payment provider or external service participates.

## What happens

The bank cash account is debit-normal; Alice's and Bob's wallets are
credit-normal liabilities. All amounts are integer USD cents.

| Step | Cash | Alice | Bob | Check |
| --- | ---: | ---: | ---: | --- |
| Deposit $100 to Alice | 10000 | 10000 | 0 | Balanced debit cash / credit wallet |
| Alice transfers $25 to Bob | 10000 | 7500 | 2500 | A new immutable transaction |
| Retry the identical transfer/key | 10000 | 7500 | 2500 | Same transaction ID and `Idempotent-Replayed: true` |
| Bob attempts a $1000 transfer | 10000 | 7500 | 2500 | HTTP 422; balances unchanged |
| Reverse Alice's $25 transfer | 10000 | 10000 | 0 | A linked reversal; original entries unchanged |
| Read balances at the transfer timestamp | 10000 | 7500 | 2500 | Historical state survives the reversal |

The final reconciliation must report **3 accounts, 3 transactions, 6 entries**,
matching cached balances and a zero USD trial balance. A replay or rejected
request must not add journal entries. The example raises an error instead of
printing a success receipt if any invariant fails.

The receipt includes actual account/transaction IDs, replay and rejection
results, historical/current balances, each checked invariant, reconciliation
results and cleanup evidence. IDs, timestamps, process IDs and ports vary on
each run. The temporary API key is omitted.

To save a receipt for inspection:

```bash
uv run python examples/wallet_demo.py > /tmp/ledgerline-wallet-receipt.json
```

## Ownership and time limits

The parent creates a `TemporaryDirectory` containing its SQLite database and
binds an IPv4 socket to **127.0.0.1, port 0**. The operating system selects an
unused port. The bound socket is passed directly to a spawned child process;
there is no release-and-reacquire port gap and no discovery of existing servers.

The child migrates that database and builds the app with explicit settings and
`_env_file=None`. A caller's `DATABASE_URL`, `.env`, API keys and running service
are not selected. The example accepts no external URL, database or port option.
It requires neither Docker nor PostgreSQL, and it leaves the production service
configuration unchanged.

Startup/migration readiness and HTTP requests share a **15-second** deadline.
Each HTTP call also has a timeout of at most one second. To choose a different
finite positive deadline:

```bash
uv run python examples/wallet_demo.py --timeout 20
```

The context manager runs cleanup on success, startup failure and workflow
exceptions. It terminates and joins its own child, escalates to killing that
same process if necessary, and bounds each join to two seconds. It closes its
owned listener and deletes its own temporary directory. A receipt is emitted
only after this cleanup completes; expected lifecycle rejection is part of a
successful run. A deadline or failed invariant exits 1 with an error on stderr.
Invalid CLI syntax uses argparse's exit 2.

Reconciliation runs locally against the owned SQLite database after all HTTP
writes finish. It demonstrates the existing journal cross-check; it is not an
additional HTTP endpoint. This small sequential demo demonstrates lifecycle
mechanics. The existing [concurrency tests](../tests/test_concurrency.py) cover
racing writes independently.

## Verification

```bash
uv run pytest tests/test_wallet_demo.py -q
```

These tests run actual HTTP, inspect the receipt, preserve a caller-owned
sentinel database, retain an unrelated listener, and check process/socket/file
cleanup after both an explicit workflow exception and a startup deadline. They
use the same disposable SQLite setup as the documented command and require no existing
server.
