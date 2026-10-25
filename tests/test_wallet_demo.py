"""Real loopback HTTP and ownership boundaries for the disposable demo."""

import importlib
import json
import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "wallet_demo.py"


def demo_module() -> ModuleType:
    return importlib.import_module("examples.wallet_demo")


def assert_resources_gone(server: Any) -> None:
    # The public receipt reports ownership; direct tests also inspect OS resources.
    pid = server.server_pid
    assert pid not in {child.pid for child in multiprocessing.active_children()}
    assert not server.database_path.exists()
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", server.port)) != 0


def test_actual_cli_wallet_receipt_and_caller_database_isolation(tmp_path: Path) -> None:
    caller_db = tmp_path / "caller.db"
    sentinel = b"caller-owned database must stay untouched"
    caller_db.write_bytes(sentinel)
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{caller_db}\n")
    environment = dict(os.environ, DATABASE_URL=f"sqlite:///{caller_db}")
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and repository-owned script
        [sys.executable, str(EXAMPLE)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )
    receipt = json.loads(result.stdout)
    ids = receipt["transaction_ids"]
    assert ids["transfer"] == ids["replay"]
    assert ids["reversal"] != ids["transfer"]
    assert receipt["after_transfer"] == {"cash": 10000, "alice": 7500, "bob": 2500}
    assert receipt["after_replay"] == receipt["after_transfer"]
    assert receipt["overdraft"]["status"] == 422
    assert receipt["overdraft"]["before"] == receipt["overdraft"]["after"]
    assert receipt["historical"]["balances"] == {"cash": 10000, "alice": 7500, "bob": 2500}
    assert receipt["current"] == {"cash": 10000, "alice": 10000, "bob": 0}
    assert all(receipt["invariants"].values())
    report = receipt["reconciliation"]
    assert report["ok"]
    assert (report["accounts"], report["transactions"], report["entries"]) == (3, 3, 6)
    assert report["trial_balance"] == {"USD": 0}
    assert receipt["cleanup"]["server_stopped"]
    assert receipt["cleanup"]["database_removed"]
    assert not Path(receipt["cleanup"]["database_path"]).exists()
    assert caller_db.read_bytes() == sentinel
    assert "Bearer" not in result.stdout


def test_real_http_context_error_cleans_only_its_resources() -> None:
    demo = demo_module()
    # An unrelated listener remains open while the owned server comes and goes.
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        unrelated_port = unrelated.getsockname()[1]
        server = demo.DemoServer()

        def fail_workflow() -> None:
            with server:
                assert server.port != unrelated_port
                assert server.request("GET", "/readyz").status == 200
                raise RuntimeError("workflow failed")

        with pytest.raises(RuntimeError, match="workflow failed"):
            fail_workflow()
        assert_resources_gone(server)
        with socket.create_connection(("127.0.0.1", unrelated_port), timeout=0.2):
            pass
        assert unrelated.fileno() >= 0


def test_startup_timeout_cleans_spawned_process_and_temporary_database() -> None:
    demo = demo_module()
    server = demo.DemoServer(timeout=0.000001)
    with pytest.raises(demo.DemoError, match="deadline"), server:
        pytest.fail("a one-microsecond startup deadline cannot be ready")
    assert server.server_pid is not None
    assert_resources_gone(server)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_before_allocating_resources(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        demo_module().DemoServer(timeout=timeout)
