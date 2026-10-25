# Disposable wallet lifecycle demo

Add one runnable example that exercises the real FastAPI service over loopback HTTP. The example owns a temporary SQLite database, an ephemeral loopback listener and a spawned server process. The child runs the existing Alembic migration and application factory with explicit settings; neither DATABASE_URL nor .env selects its database. No Docker, PostgreSQL, payment integration or pre-existing server is required.

Use a parent-bound socket passed through multiprocessing spawn, avoiding a reserve/release port race. Startup and requests share a finite deadline; shutdown terminates and joins only the owned process, escalating to kill with a bounded join. Context-manager cleanup runs on success, startup failure and workflow exceptions. The parent deletes only its TemporaryDirectory. A random ephemeral API key stays out of the receipt.

Demonstrate cash/Alice/Bob accounts, $100 deposit, $25 transfer, exact-key replay, rejected overdraft, reversal, historical/current balances and existing reconciliation. Validate the invariants before printing a JSON receipt containing real IDs, integer-cent balances, expected rejection and cleanup evidence. Reconciliation uses the existing local query API after the HTTP writes finish. IDs/timestamps vary because this is a real disposable run.

A context-managed spawned process is selected over a daemon thread (which cannot be reliably stopped after a startup error) or connecting to a caller-provided URL/database (which risks touching unrelated state). Keep the production service unchanged. Real HTTP tests verify lifecycle invariants, caller database isolation, independent listener ownership and cleanup after both a body exception and a startup timeout.
