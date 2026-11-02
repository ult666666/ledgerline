# Wallet demo

From a source checkout after `uv sync`, run:

```bash
uv run python examples/wallet_demo.py
```

The script starts the API on a temporary SQLite database, runs a short wallet
lifecycle over HTTP, checks the accounting and retry rules after each step, cleans
up, and prints a JSON summary. It uses the project's Alembic migrations, application
factory and reconciliation code. Every account and amount is local to the run; no
payment provider or other service is involved.

## What happens

The bank's cash account is debit-normal; Alice's and Bob's wallets are
credit-normal liabilities. All amounts are integer USD cents.

| Step | Cash | Alice | Bob | Check |
| --- | ---: | ---: | ---: | --- |
| Deposit $100 to Alice | 10000 | 10000 | 0 | Balanced debit cash / credit wallet |
| Alice transfers $25 to Bob | 10000 | 7500 | 2500 | A new immutable transaction |
| Retry the identical transfer/key | 10000 | 7500 | 2500 | Same transaction ID and `Idempotent-Replayed: true` |
| Bob attempts a $1000 transfer | 10000 | 7500 | 2500 | HTTP 422; balances unchanged |
| Reverse Alice's $25 transfer | 10000 | 10000 | 0 | A linked reversal; original entries unchanged |
| Read balances at the transfer timestamp | 10000 | 7500 | 2500 | Historical state survives the reversal |

At the end, reconciliation must find 3 accounts, 3 transactions and 6 entries,
cached balances that match the entries, and a zero USD trial balance. The replay and
the refused transfer must not add journal entries. If any check fails, the script
raises an error instead of printing a summary.

The summary lists the account and transaction IDs, the replay and rejection results,
historical and current balances, each check, the reconciliation result and the
cleanup status. IDs, timestamps, process IDs and ports change from run to run, and the
temporary API key is left out. To keep a copy:

```bash
uv run python examples/wallet_demo.py > /tmp/ledgerline-wallet-demo.json
```

## Isolation and time limits

The script creates a temporary directory for its SQLite database and binds a socket
to 127.0.0.1 on port 0, so the operating system picks a free port. It passes that
socket straight to a child process, so there is no gap in which another program could
take the port, and it never connects to a server that was already running.

The child migrates the temporary database and builds the app with explicit settings
and `_env_file=None`, so your `DATABASE_URL`, `.env` file and API keys are ignored.
The script takes no URL, database or port option, and it needs neither Docker nor
PostgreSQL.

Startup, migration and the HTTP requests share a 15-second deadline, and each HTTP
call times out after at most one second. To choose another deadline:

```bash
uv run python examples/wallet_demo.py --timeout 20
```

Cleanup runs after success, a startup failure or an error mid-run. The script stops
the child process (killing it if it does not exit), waits at most two seconds each
time, closes the socket and deletes the temporary directory. It prints the summary
only after cleanup. The refused transfer is an expected part of a successful run. A
missed deadline or a failed check exits with status 1 and an error on stderr; invalid
arguments exit with status 2.

Reconciliation runs in the script against the temporary database after the last HTTP
write; it is not an HTTP endpoint. The demo is sequential and only shows the
lifecycle. The [concurrency tests](../tests/test_concurrency.py) cover racing writes.

## Tests

```bash
uv run pytest tests/test_wallet_demo.py -q
```

These tests run the script over HTTP and inspect its summary. They check that the
database named in your `DATABASE_URL` and `.env` is left untouched, that an unrelated
listening socket stays open, and that the child process, socket and temporary files
are gone after both a failure mid-run and a missed startup deadline. Like the script,
they need no running server.
