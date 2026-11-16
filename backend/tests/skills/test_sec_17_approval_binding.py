"""SECURITY_REVIEW #17: the approval shows the exact MCP command and is bound to the content hash of the archive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skills_helpers import approve_pending, entry, make_manager, make_repo, simple_skill_files, write_catalog

from orchestrator.skills.errors import ActivationRefused
from orchestrator.testing import make_harness

MCP = dict(kind="mcp_server", install_method={"type": "mcp_npx", "command": "claude mcp add -e API_KEY=xxx -e OTHER=y playwright npx @playwright/mcp@latest", "notes": "n"})


@pytest.fixture
async def h(tmp_path):
    harness = await make_harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


async def setup(h, tmp_path, files=None, entries=None):
    repo = tmp_path / "repos" / "s1"
    make_repo(repo, files or simple_skill_files("s1"))
    m = await make_manager(h, tmp_path, {"s1": repo}, platform="win32")
    await m.import_catalog(write_catalog(tmp_path / "cat.yaml", entries or [entry("s1")]))
    proj = await h.project("p1", git_repo=False, files={"keep.txt": "mine\n"})
    return m, repo, proj


async def test_mcp_approval_shows_exact_command_args_and_env_keys(h, tmp_path):
    m, repo, proj = await setup(h, tmp_path, {"README.md": "mcp\n"}, [entry("s1", **MCP)])
    await m.install("s1")
    status, body = await m.activate("s1", proj.id)
    assert status == 202
    appr = body["approval"]
    act = appr["details"]["activation"]
    assert act["command"] == "cmd" and act["args"] == ["/c", "npx", "@playwright/mcp@latest"]
    assert act["env_keys"] == ["API_KEY", "OTHER"] and act["file"] == ".mcp.json" and act["server"] == "playwright"
    assert "cmd /c npx @playwright/mcp@latest" in appr["summary"] and "API_KEY" in appr["summary"]
    assert len(appr["details"]["content_sha256"]) == 64 and len(appr["details"]["mcp_entry_sha256"]) == 64
    assert not (Path(proj.path) / ".mcp.json").exists()


async def test_archive_changed_after_approval_is_refused_and_approval_invalidated(h, tmp_path):
    m, repo, proj = await setup(h, tmp_path)
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    archive = Path((await m.get_row("s1"))["local_path"])
    (archive / "SKILL.md").write_text("---\nname: s1\n---\nEVIL changed after approval\n")  # content changes after approval
    with pytest.raises(ActivationRefused):
        await m.activate("s1", proj.id)
    assert not (Path(proj.path) / ".claude").exists()
    assert (await m.get_row("s1"))["project_scopes"] == []
    approvals = [a for a in await h.approvals.list() if a.kind == "untrusted_tool_install"]
    assert approvals and all(a.status == "denied" for a in approvals)
    assert any("invalidated" in (a.note or "") for a in approvals)


async def test_content_swapped_between_approval_check_and_write_is_caught(h, tmp_path):
    """Approval is valid for the content at approval time; swapping the archive afterwards must not activate."""
    m, repo, proj = await setup(h, tmp_path)
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    skill = await m.get_row("s1")
    archive = Path(skill["local_path"])
    (archive / "added.sh").write_text("echo pwned\n")
    # even calling the internal apply step directly (as rollback does) re-verifies the hash
    info = m.activation_info(skill)
    project = proj
    with pytest.raises(ActivationRefused):
        await m._apply_activation(skill, project, "project", ["claude"], info, body["approval_id"])
    assert not (Path(proj.path) / ".claude").exists()


async def test_mcp_command_changed_in_catalog_after_approval_is_refused(h, tmp_path):
    m, repo, proj = await setup(h, tmp_path, {"README.md": "mcp\n"}, [entry("s1", **MCP)])
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    evil = dict(MCP, install_method={"type": "mcp_npx", "command": "claude mcp add playwright npx evil-package", "notes": "n"})
    await m.import_catalog(write_catalog(tmp_path / "cat2.yaml", [entry("s1", **evil)]))
    status, body2 = await m.activate("s1", proj.id)
    assert status == 202 and body2["approval_id"] != body["approval_id"]  # the old approval no longer covers it
    assert not (Path(proj.path) / ".mcp.json").exists()
    assert body2["approval"]["details"]["activation"]["args"][-1] == "evil-package"


async def test_unchanged_content_activates_normally_and_hash_recorded(h, tmp_path):
    m, repo, proj = await setup(h, tmp_path)
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    status, out = await m.activate("s1", proj.id)
    assert status == 200 and (Path(proj.path) / ".claude" / "skills" / "s1" / "SKILL.md").is_file()
    archives = (await m.get_row("s1"))["install_method"]["local"]["archives"]
    assert archives[0]["tree_sha256"] == body["approval"]["details"]["content_sha256"]


async def test_legacy_approval_without_hash_does_not_authorise(h, tmp_path):
    m, repo, proj = await setup(h, tmp_path)
    await m.install("s1")
    sha = (await m.get_row("s1"))["installed_ref"]
    legacy = await h.approvals.request("untrusted_tool_install", "legacy", project_id=proj.id,
                                       details={"skill_id": "s1", "ref": sha, "project_id": proj.id})
    await approve_pending(h, legacy.id)
    status, body = await m.activate("s1", proj.id)
    assert status == 202 and body["approval_id"] != legacy.id
    assert not (Path(proj.path) / ".claude").exists()
