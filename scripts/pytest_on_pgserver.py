"""Run the test suite against a throwaway PostgreSQL server, without Docker.

Uses the ``pgserver`` package, which bundles PostgreSQL binaries for macOS and
Linux. It is not a project dependency; run this script with

    uv run --with pgserver python scripts/pytest_on_pgserver.py [pytest arguments]

It starts PostgreSQL in a temporary directory, creates a ``ledgerline_test``
database, applies the Alembic migrations, runs pytest with DATABASE_URL
pointing at that database, and stops the server again.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ledgerline-pg-") as data_dir:
        server = pgserver.get_server(Path(data_dir), cleanup_mode="stop")
        try:
            print(server.psql("SELECT version();"), flush=True)
            server.psql("CREATE DATABASE ledgerline_test;")
            # A postgresql:// URL over a Unix socket; ledgerline.config switches
            # it to the psycopg driver.
            env = {**os.environ, "DATABASE_URL": server.get_uri("ledgerline_test")}
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env
            )
            return subprocess.run(
                [sys.executable, "-m", "pytest", *sys.argv[1:]], env=env
            ).returncode
        finally:
            server.cleanup()


if __name__ == "__main__":
    sys.exit(main())
