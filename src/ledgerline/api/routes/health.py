"""Liveness and readiness probes (unauthenticated)."""

from fastapi import APIRouter

from ledgerline.api.dependencies import ServiceDep

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness: the process is up")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness: the database answers queries")
def readyz(service: ServiceDep) -> dict[str, str]:
    service.check_database()
    return {"status": "ready"}
