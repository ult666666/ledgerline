"""Cross-cutting HTTP behaviour: authentication, problem responses, health and request IDs."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ledgerline.api.app import create_app
from ledgerline.api.auth import authenticate_api_key, key_fingerprint
from ledgerline.config import Settings
from ledgerline.errors import ConcurrentUpdate
from tests.support import API_KEY, problem_type


@pytest.fixture
def anonymous(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


class TestAuthentication:
    def test_missing_credentials_are_rejected(self, anonymous: TestClient) -> None:
        response = anonymous.get("/v1/accounts")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert problem_type(response) == "authentication-required"

    @pytest.mark.parametrize(
        "authorization", ["Bearer wrong-key", f"Basic {API_KEY}", API_KEY, "Bearer"]
    )
    def test_wrong_credentials_are_rejected(
        self, anonymous: TestClient, authorization: str
    ) -> None:
        response = anonymous.get("/v1/accounts", headers={"Authorization": authorization})
        assert response.status_code == 401

    def test_a_configured_key_is_accepted(self, anonymous: TestClient) -> None:
        response = anonymous.get("/v1/accounts", headers={"Authorization": f"Bearer {API_KEY}"})
        assert response.status_code == 200

    def test_without_configured_keys_nothing_authenticates(
        self, settings: Settings, db: Engine
    ) -> None:
        locked = create_app(settings.model_copy(update={"api_keys": []}), engine=db)
        with TestClient(locked) as client:
            response = client.get("/v1/accounts", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 401

    def test_key_matching(self) -> None:
        keys = ["first-key", "second-key"]
        assert authenticate_api_key("second-key", keys) == key_fingerprint("second-key")
        assert authenticate_api_key("second-ke", keys) is None
        assert authenticate_api_key("anything", []) is None
        assert len(key_fingerprint("first-key")) == 16
        assert key_fingerprint("first-key") != key_fingerprint("second-key")


class TestHealth:
    def test_liveness_needs_no_credentials(self, anonymous: TestClient) -> None:
        assert anonymous.get("/healthz").json() == {"status": "ok"}

    def test_readiness_checks_the_database(self, anonymous: TestClient) -> None:
        assert anonymous.get("/readyz").json() == {"status": "ready"}

    def test_readiness_fails_when_the_database_is_unreachable(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        missing_directory = tmp_path_factory.getbasetemp() / "does-not-exist"
        broken = create_app(
            Settings(
                database_url=f"sqlite:///{missing_directory}/ledger.db",
                api_keys=[API_KEY],
                _env_file=None,
            )
        )
        with TestClient(broken) as client:
            response = client.get("/readyz")
        assert response.status_code == 503
        assert problem_type(response) == "database-unavailable"


class TestProblemResponses:
    def test_unknown_route(self, client: TestClient) -> None:
        response = client.get("/v1/nothing-here")
        assert response.status_code == 404
        body = response.json()
        assert body["type"] == "about:blank"
        assert body["title"] == "Not Found"
        assert "detail" not in body

    def test_wrong_method(self, client: TestClient) -> None:
        response = client.delete("/v1/accounts")
        assert response.status_code == 405
        assert response.headers["content-type"] == "application/problem+json"
        assert "allow" in response.headers

    def test_malformed_json(self, client: TestClient) -> None:
        response = client.post(
            "/v1/accounts", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert problem_type(response) == "validation-error"

    def test_unexpected_errors_become_a_500_problem_with_the_request_id(self, app: FastAPI) -> None:
        @app.get("/boom")
        def boom() -> None:
            raise RuntimeError("something internal")

        with TestClient(app) as client:
            response = client.get("/boom")
        assert response.status_code == 500
        body = response.json()
        assert body["title"] == "Internal Server Error"
        assert "something internal" not in response.text
        assert body["request_id"] == response.headers["X-Request-ID"]

    def test_concurrency_conflicts_ask_the_client_to_retry(self, app: FastAPI) -> None:
        @app.get("/contended")
        def contended() -> None:
            raise ConcurrentUpdate("try again")

        with TestClient(app) as client:
            response = client.get("/contended")
        assert response.status_code == 409
        assert response.headers["Retry-After"] == "1"
        assert problem_type(response) == "concurrent-update"


class TestRequestIds:
    def test_a_request_id_is_generated(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert len(response.headers["X-Request-ID"]) == 32

    def test_a_well_formed_incoming_request_id_is_kept(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"X-Request-ID": "trace-abc.123"})
        assert response.headers["X-Request-ID"] == "trace-abc.123"

    def test_a_suspicious_incoming_request_id_is_replaced(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
        assert response.headers["X-Request-ID"] != "bad id\twith spaces"

    def test_problem_bodies_carry_the_request_id(self, client: TestClient) -> None:
        response = client.get("/v1/nothing-here", headers={"X-Request-ID": "req-42"})
        assert response.json()["request_id"] == "req-42"
