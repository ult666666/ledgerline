"""Application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import Engine

from ledgerline import __version__
from ledgerline.api.dependencies import authenticate
from ledgerline.api.middleware import RequestContextMiddleware
from ledgerline.api.problems import install_problem_handlers
from ledgerline.api.routes import accounts, health, transactions
from ledgerline.config import Settings
from ledgerline.db import create_db_engine, create_session_factory
from ledgerline.observability import configure_logging
from ledgerline.service import LedgerService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    """Build the API. Pass ``engine`` to share one (e.g. in tests); the caller then owns it."""
    settings = settings or Settings()
    configure_logging(settings.log_level)
    owns_engine = engine is None
    db_engine = engine if engine is not None else create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not settings.api_keys:
            logger.warning("LEDGERLINE_API_KEYS is empty: every /v1 request will get a 401")
        yield
        if owns_engine:
            db_engine.dispose()

    app = FastAPI(
        title="ledgerline",
        version=__version__,
        summary="A double-entry ledger with idempotent writes and race-free balance checks.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = LedgerService(create_session_factory(db_engine))
    app.add_middleware(RequestContextMiddleware)
    install_problem_handlers(app)

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
    v1.include_router(accounts.router)
    v1.include_router(transactions.router)
    app.include_router(health.router)
    app.include_router(v1)
    return app
