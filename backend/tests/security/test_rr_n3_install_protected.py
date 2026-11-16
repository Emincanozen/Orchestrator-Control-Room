"""Re-review N3: the orchestrator's own installation is protected like the data dir.

`validate_root` accepted the checkout's (non-git) parent directory (-> `in_place`: agents would work in a
tree containing `config/policies.yaml`, the backend source and the venv) and the checkout itself. A worker
editing `config/policies.yaml` could switch on `claude_shell` / `auto_approve`. Protected now, for project
registration AND for every run's cwd / extra dirs: data dir, worktrees dir (registration only), the checkout,
the config dir (also an ORCHESTRATOR_CONFIG_DIR elsewhere), the venv / interpreter, the dashboard build."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator import config
from orchestrator.api.app import create_app
from orchestrator.domain.states import TaskState
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.mock import MockProvider, MockScript
from orchestrator.providers.process import InvalidRequestError
from orchestrator.security.paths import PathPolicyError, validate_root
from orchestrator.testing import make_harness


def _req(cwd: str, extra=()) -> ExecutionRequest:
    return ExecutionRequest(run_id="r", prompt="p", cwd=cwd, extra_dirs=list(extra), permission="read_only")


def test_checkout_parent_and_install_dirs_cannot_be_registered():
    for d in (config.REPO_ROOT, config.REPO_ROOT.parent, config.BACKEND_DIR, config.CONFIG_DIR, config.PACKAGE_DIR,
              Path(sys.prefix)):
        if d.is_dir():
            with pytest.raises(PathPolicyError, match="installation"):
                validate_root(str(d))


def test_config_dir_elsewhere_is_protected(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "proj" / "orch-config"
    cfg.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    assert validate_root(str(tmp_path / "proj")) == (tmp_path / "proj").resolve()  # control
    monkeypatch.setenv("ORCHESTRATOR_CONFIG_DIR", str(cfg))
    for d in (tmp_path / "proj", cfg):  # contains / is
        with pytest.raises(PathPolicyError, match="installation"):
            validate_root(str(d))
    assert validate_root(str(other)) == other.resolve()


def test_registration_api_refuses_the_checkout_and_its_parent(tmp_path: Path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {app.state.rt.token}"}
        for d in (config.REPO_ROOT, config.REPO_ROOT.parent):
            r = c.post("/api/projects", json={"name": "x", "path": str(d)}, headers=h)
            assert r.status_code in (400, 403, 422), (d, r.status_code, r.text)
            assert "installation" in r.text, r.text


def test_runs_in_or_around_the_installation_are_refused(tmp_path: Path):
    adapter = CodexAdapter(data_dir=tmp_path / "data")
    for cwd in (config.REPO_ROOT, config.REPO_ROOT.parent, config.BACKEND_DIR, config.REPO_ROOT / "config"):
        with pytest.raises(InvalidRequestError, match="protected orchestrator directory"):
            adapter.check_request(_req(str(cwd)))
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(InvalidRequestError, match="protected orchestrator directory"):
        adapter.check_request(_req(str(work), extra=[str(config.CONFIG_DIR)]))
    with pytest.raises(InvalidRequestError, match="protected orchestrator directory"):
        adapter.check_request(_req(str(work), extra=[sys.prefix]))
    adapter.check_request(_req(str(work)))  # an ordinary folder is fine


@pytest.mark.parametrize("git_repo", [True, False], ids=["worktree", "in_place"])
async def test_existing_project_row_pointing_at_a_now_protected_root_is_refused_at_run_time(tmp_path: Path, monkeypatch, git_repo):
    """A row registered before N3 (simulated: the project becomes protected after registration)."""
    h = await make_harness(tmp_path)
    try:
        p = await h.project(git_repo=git_repo)
        cfg = Path(p.path) / "cfg"
        cfg.mkdir()
        monkeypatch.setenv("ORCHESTRATOR_CONFIG_DIR", str(cfg))  # the project now contains the config dir
        h.mock.queue(MockScript.success("wrote", files={"x.txt": "x"}))
        t = await h.task(p.id, verify_commands=[{"file_exists": "x.txt"}], autostart=True)
        state = await h.run_once(t.id)
        t = await h.tasks.get(t.id)
        assert state in (TaskState.BLOCKED, TaskState.FAILED), state
        assert "project path refused" in (t.state_reason or "") and "protected orchestrator directory" in t.state_reason
        assert h.mock.requests == []  # the provider never ran
        assert not (Path(p.path) / "x.txt").exists()
    finally:
        await h.close()
