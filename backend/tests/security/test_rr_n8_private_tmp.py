"""Re-review N8: the orchestrator's own temp files (Codex `--output-schema`, verification logs) were created in
%TEMP%, which sandboxed Codex tool commands can write. They now live in `<data dir>/tmp` (owner-only, inside
the protected data dir), emptied at every server start."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.config import get_paths
from orchestrator.providers import codex as codex_mod
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.mock import MockProvider
from orchestrator.scheduler import verification
from orchestrator.scheduler.verification import Verifier, run_command
from orchestrator.services.context import AppContext


def _req(cwd: str) -> ExecutionRequest:
    return ExecutionRequest(run_id="r", prompt="p", cwd=cwd, permission="read_only", json_schema={"type": "object"})


def test_codex_schema_file_is_in_the_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(codex_mod.SCHEMA_IN_TEMP_ENV, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    data = tmp_path / "data"
    spec = CodexAdapter(data_dir=data).build_command(_req(str(work)))
    schema = Path(spec.args[spec.args.index("--output-schema") + 1])
    assert schema.parent == data / "tmp" and schema.name.startswith("orch-schema-")
    assert schema.read_text(encoding="utf-8") == '{"type": "object"}'
    assert schema in spec.cleanup


def test_codex_schema_fallback_switch(tmp_path: Path, monkeypatch):
    """Escape hatch in case a real Codex run cannot read the file in the data dir (unverified: no real run)."""
    monkeypatch.setenv(codex_mod.SCHEMA_IN_TEMP_ENV, "1")
    work = tmp_path / "work"
    work.mkdir()
    spec = CodexAdapter(data_dir=tmp_path / "data").build_command(_req(str(work)))
    schema = Path(spec.args[spec.args.index("--output-schema") + 1])
    assert os.path.normcase(str(schema.parent)) == os.path.normcase(os.path.realpath(__import__("tempfile").gettempdir()))
    for p in spec.cleanup:
        p.unlink()


def test_verification_log_is_in_the_data_dir(tmp_path: Path, monkeypatch):
    from security.test_n2_verification_sandbox import Env

    instance = Env(tmp_path)
    seen: list[str] = []
    real = verification.tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real(*a, **kw)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(verification.tempfile, "mkstemp", spy)
    paths = instance.paths
    ctx = instance.ctx
    try:
        status, results = asyncio.run(Verifier(ctx).run_checks([{"cmd": [sys.executable, "-c", "print(1)"]}], str(instance.wt)))
    finally:
        ctx.close()
    assert status == "passed", results
    assert seen and all(Path(n).parent == paths.tmp_dir for n in seen), seen
    # direct callers without tmp_dir keep the system temp dir
    seen.clear()
    r = asyncio.run(run_command([sys.executable, "-c", "pass"], cwd=str(instance.wt), timeout=30,
                               env=dict(os.environ), writable_cwd=True))
    assert r.passed and seen and Path(seen[0]).parent != paths.tmp_dir


def test_tmp_dir_is_emptied_at_startup_and_protected(tmp_path: Path):
    data = tmp_path / "data"
    stale = data / "tmp" / "orch-schema-stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    app = create_app(data_dir=data, providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app):
        assert (data / "tmp").is_dir()
        assert not stale.exists()
    # inside the data dir, so already covered by check_run_paths / validate_root
    assert get_paths(data).tmp_dir.parent == get_paths(data).data_dir
