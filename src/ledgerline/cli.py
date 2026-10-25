"""Command-line entry point: serve, reconcile and secure bearer-key generation."""

import argparse
import secrets
import sys
from collections.abc import Sequence

import uvicorn
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerline.api.app import create_app
from ledgerline.config import Settings
from ledgerline.db import create_db_engine
from ledgerline.reconcile import reconcile


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate-key":
        # 32 bytes of OS randomness, URL-safe for use as a bearer token.
        # Do not read application configuration or log the generated secret.
        print(secrets.token_urlsafe(32))
        return 0
    settings = Settings()
    if args.command == "serve":
        return _serve(settings, host=args.host, port=args.port)
    return _reconcile(settings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledgerline", description="Double-entry ledger service.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the HTTP API with uvicorn")
    serve.add_argument("--host", default="127.0.0.1", help="address to bind (default: %(default)s)")
    serve.add_argument("--port", type=int, default=8000, help="port (default: %(default)s)")
    commands.add_parser(
        "reconcile",
        help="check stored balances and the trial balance; exits 1 if anything is off",
    )
    commands.add_parser("generate-key", help="print a new random bearer token; keep it secret")
    return parser


def _serve(settings: Settings, *, host: str, port: int) -> int:
    app = create_app(settings)
    # log_config=None keeps uvicorn from installing its plain-text handlers, so
    # its messages flow through our JSON logging; the request middleware already
    # logs one line per request, so uvicorn's access log would be a duplicate.
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    return 0


def _reconcile(settings: Settings) -> int:
    engine = create_db_engine(settings.database_url)
    try:
        with Session(engine) as session:
            report = reconcile(session)
    except SQLAlchemyError as exc:
        first_line = str(exc).splitlines()[0]
        print(
            f"error: could not read the ledger (has `alembic upgrade head` run?): {first_line}",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()
    print(report.render())
    return 0 if report.ok else 1
