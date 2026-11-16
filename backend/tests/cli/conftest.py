"""Fixtures for the CLI tests: a real in-process app (mock provider) reached through the CLI's injectable HTTP client."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from orchestrator.api.app import create_app
from orchestrator.cli import client as cli_client
from orchestrator.cli.main import app as cli_app
from orchestrator.providers.mock import MockProvider
from orchestrator.testing import make_git_repo

BASE = "http://127.0.0.1:8765"
PY_OK = f'"{sys.executable}" -c "print(1)"'


@dataclass
class Env:
    http: TestClient
    token: str
    mock: MockProvider
    tmp: Any
    runner: CliRunner

    def run(self, *args: str, check_token: bool = True) -> Any:
        result = self.runner.invoke(cli_app, list(args))
        if check_token:  # the token must never reach any output stream
            assert self.token not in (result.stdout or ""), "token leaked to stdout"
            assert self.token not in (result.stderr or ""), "token leaked to stderr"
        return result

    def ok(self, *args: str) -> Any:
        r = self.run(*args)
        assert r.exit_code == 0, f"{args}: exit {r.exit_code}\nstdout={r.stdout}\nstderr={r.stderr}"
        return r

    def json(self, *args: str) -> Any:
        return json.loads(self.ok(*args, "--json").stdout)

    def project(self, name: str = "demo", verify: Optional[list] = None, **kw: Any) -> dict:
        repo = make_git_repo(self.tmp / f"repo-{name}")
        body = {"name": name, "path": str(repo), "verify_commands": verify if verify is not None else [[sys.executable, "-c", "print('ok')"]]}
        r = self.http.post("/api/projects", json={**body, **kw})
        assert r.status_code == 201, r.text
        return r.json()

    def wait_state(self, task_id: str, states: set[str], timeout: float = 30.0) -> dict:
        end = time.monotonic() + timeout
        t: dict = {}
        while time.monotonic() < end:
            t = self.http.get(f"/api/tasks/{task_id}").json()
            if t["state"] in states:
                return t
            time.sleep(0.05)
        raise AssertionError(t)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never touch the real data dir / environment: everything lives under tmp_path."""
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    for var in ("ORCHESTRATOR_URL", "ORCHESTRATOR_TOKEN_FILE", "ORCHESTRATOR_WORKTREES_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield
    cli_client.set_client_factory(None)
    cli_client._known_tokens.clear()


def build_env(tmp_path, mock: MockProvider, stack: list) -> Env:
    app = create_app(data_dir=tmp_path / "data", providers={"mock": mock}, use_routing=True, celebrate_seconds=0, poll_interval=0.05)
    http = TestClient(app, base_url=BASE)
    http.__enter__()
    stack.append(http)
    token = app.state.rt.token
    http.headers["Authorization"] = f"Bearer {token}"  # for the test's own direct calls
    assert http.put("/api/settings", json={"values": {"limits": {"retry_backoff_seconds": 0}}}).status_code == 200
    cli_client.set_client_factory(lambda base: http)
    return Env(http=http, token=token, mock=mock, tmp=tmp_path, runner=CliRunner())


@pytest.fixture
def env(tmp_path):
    stack: list = []
    yield build_env(tmp_path, MockProvider(), stack)
    for c in stack:
        c.__exit__(None, None, None)


@pytest.fixture
def make_env(tmp_path):
    stack: list = []

    def factory(mock: MockProvider) -> Env:
        return build_env(tmp_path, mock, stack)

    yield factory
    for c in stack:
        c.__exit__(None, None, None)


def canned_factory(handler: Callable[[httpx.Request], httpx.Response]):
    """Client factory answering from a handler (no app): for endpoints that are not in this build."""
    return lambda base: httpx.Client(base_url=base, transport=httpx.MockTransport(handler))
