import json
from pathlib import Path

import pytest
from skills_helpers import approve_pending, entry, make_manager, make_repo, simple_skill_files, write_catalog

from orchestrator.skills.errors import ActivationRefused, CatalogueOnly, InvalidSkillState
from orchestrator.testing import make_harness


@pytest.fixture
async def h(tmp_path):
    harness = await make_harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


async def setup(h, tmp_path, files=None, entries=None, pname="p1", **mkw):
    repo = tmp_path / "repos" / "s1"
    sha = make_repo(repo, files or simple_skill_files("s1"))
    m = await make_manager(h, tmp_path, {"s1": repo}, **mkw)
    await m.import_catalog(write_catalog(tmp_path / "cat.yaml", entries or [entry("s1")]))
    proj = await h.project(pname, git_repo=False, files={"keep.txt": "mine\n"})
    return m, repo, sha, proj


async def activate_ok(h, m, sid, proj):
    status, body = await m.activate(sid, proj.id)
    if status == 202:
        await approve_pending(h, body["approval_id"])
        status, body = await m.activate(sid, proj.id)
    assert status == 200, body
    return body


async def test_activate_without_approval_returns_202_and_changes_nothing(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    await m.install("s1")
    status, body = await m.activate("s1", proj.id)
    assert status == 202 and body["status"] == "approval_required"
    appr = [a for a in await h.approvals.list("pending") if a.kind == "untrusted_tool_install"][0]
    assert appr.id == body["approval_id"] and appr.kind == "untrusted_tool_install"
    assert appr.details["skill_id"] == "s1" and appr.details["ref"] == sha and appr.details["project_id"] == proj.id
    assert not (Path(proj.path) / ".claude").exists()
    assert (await m.get_row("s1"))["state"] == "DOWNLOADED_AND_DISABLED" and (await m.get_row("s1"))["project_scopes"] == []
    # asking again reuses the pending approval instead of creating another
    status2, body2 = await m.activate("s1", proj.id)
    assert status2 == 202 and body2["approval_id"] == body["approval_id"]
    assert len([a for a in await h.approvals.list("pending") if a.kind == "untrusted_tool_install"]) == 1


async def test_denied_approval_does_not_activate_and_new_request_is_made(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await h.approvals.resolve(body["approval_id"], False, by="user")
    status, body2 = await m.activate("s1", proj.id)
    assert status == 202 and body2["approval_id"] != body["approval_id"]
    assert not (Path(proj.path) / ".claude").exists()


async def test_activation_after_approval_copies_files(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    await m.install("s1")
    seq0 = await h.ctx.bus.last_seq()
    body = await activate_ok(h, m, "s1", proj)
    root = Path(proj.path)
    target = root / ".claude" / "skills" / "s1"
    assert tree(target) == {"SKILL.md", "notes/extra.md", ".orchestrator-skill.json"}
    assert (target / "SKILL.md").read_text() == simple_skill_files("s1")["SKILL.md"]
    marker = json.loads((target / ".orchestrator-skill.json").read_text())
    assert marker["skill_id"] == "s1" and marker["ref"] == sha
    s = await m.get_row("s1")
    assert s["state"] == "INSTALLED_AND_ACTIVE"
    rec = s["project_scopes"][0]
    assert rec["project_id"] == proj.id and rec["scope"] == "project" and rec["ref"] == sha and rec["approval_id"]
    p = await h.projects.get(proj.id)
    assert p.skill_profile["active_skills"]["s1"]["ref"] == sha
    assert tree(root) >= {"keep.txt"}
    states = [(e.payload["action"], e.payload["from_state"], e.payload["to_state"]) for e in await h.ctx.bus.replay(seq0) if e.type == "skill.state_changed"]
    assert states == [("activate", "DOWNLOADED_AND_DISABLED", "INSTALLED_AND_ACTIVE")]
    # a second activation is a no-op
    status, again = await m.activate("s1", proj.id)
    assert status == 200 and again["changed"] is False


async def test_nothing_is_active_in_project_while_skill_is_disabled(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    root = Path(proj.path)
    before = tree(root)
    await m.install("s1")  # downloaded and disabled
    assert tree(root) == before and not (root / ".claude").exists() and not (root / ".mcp.json").exists()
    await activate_ok(h, m, "s1", proj)
    await m.deactivate("s1", proj.id)
    assert tree(root) == before
    assert (await h.projects.get(proj.id)).skill_profile["active_skills"] == {}


async def test_deactivate_removes_exactly_what_was_written(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    root = Path(proj.path)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text("{}")
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    target = root / ".claude" / "skills" / "s1"
    (target / "user-added.md").write_text("the user's own file\n")
    res = await m.deactivate("s1", proj.id)
    assert (target / "user-added.md").exists() and not (target / "SKILL.md").exists() and not (target / "notes").exists()
    assert any("user-added" not in r for r in res["removed"]) and str(target) in res["left"]
    assert (root / ".claude" / "settings.local.json").exists() and (root / "keep.txt").exists()
    s = await m.get_row("s1")
    assert s["state"] == "DOWNLOADED_AND_DISABLED" and s["project_scopes"] == []
    ev = [e["action"] for e in await h.ctx.db.all("SELECT * FROM skill_events WHERE skill_id='s1' ORDER BY ts")]
    assert ev == ["download", "activate", "deactivate"]
    with pytest.raises(InvalidSkillState):
        await m.deactivate("s1", proj.id)


async def test_clean_deactivate_leaves_no_trace_dirs(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    root = Path(proj.path)
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    await m.deactivate("s1", proj.id)
    assert not (root / ".claude").exists()


async def test_refuses_to_overwrite_foreign_folder(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    foreign = Path(proj.path) / ".claude" / "skills" / "s1"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("the user's own skill\n")
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    with pytest.raises(ActivationRefused):
        await m.activate("s1", proj.id)
    assert (foreign / "SKILL.md").read_text() == "the user's own skill\n"
    assert (await m.get_row("s1"))["state"] == "DOWNLOADED_AND_DISABLED"


async def test_name_conflict_with_other_managed_skill_refused(h, tmp_path):
    r1, r2 = tmp_path / "repos" / "a", tmp_path / "repos" / "b"
    make_repo(r1, simple_skill_files("same"))
    make_repo(r2, simple_skill_files("same"))
    m = await make_manager(h, tmp_path, {"a": r1, "b": r2})
    await m.import_catalog(write_catalog(tmp_path / "c.yaml", [entry("a"), entry("b")]))
    proj = await h.project("p1", git_repo=False)
    for sid in ("a", "b"):
        await m.install(sid)
    await activate_ok(h, m, "a", proj)
    _, body = await m.activate("b", proj.id)
    await approve_pending(h, body["approval_id"])
    with pytest.raises(ActivationRefused):
        await m.activate("b", proj.id)
    assert (Path(proj.path) / ".claude" / "skills" / "same" / ".orchestrator-skill.json").is_file()


async def test_needs_review_cleared_by_approval(h, tmp_path):
    files = {**simple_skill_files("s1"), "scripts/x.sh": "echo\n"}
    m, repo, sha, proj = await setup(h, tmp_path, files)
    await m.install("s1")
    assert (await m.get_row("s1"))["state"] == "NEEDS_SECURITY_REVIEW"
    status, body = await m.activate("s1", proj.id)
    assert status == 202 and body["approval"]["details"]["review_reasons"]
    assert (await m.get_row("s1"))["state"] == "NEEDS_SECURITY_REVIEW" and not (Path(proj.path) / ".claude").exists()
    await approve_pending(h, body["approval_id"])
    status, _ = await m.activate("s1", proj.id)
    assert status == 200 and (await m.get_row("s1"))["state"] == "INSTALLED_AND_ACTIVE"
    acts = [(e["action"], e["to_state"]) for e in await h.ctx.db.all("SELECT * FROM skill_events WHERE skill_id='s1' ORDER BY ts")]
    assert acts == [("download", "NEEDS_SECURITY_REVIEW"), ("review_cleared", "DOWNLOADED_AND_DISABLED"), ("activate", "INSTALLED_AND_ACTIVE")]
    # the script was copied as a file but nothing was executed or made executable
    assert (Path(proj.path) / ".claude" / "skills" / "s1" / "scripts" / "x.sh").exists()


async def test_activate_requires_download_and_known_project(h, tmp_path):
    from orchestrator.skills.errors import SkillNotFound

    m, repo, sha, proj = await setup(h, tmp_path)
    with pytest.raises(InvalidSkillState):
        await m.activate("s1", proj.id)
    await m.install("s1")
    with pytest.raises(SkillNotFound):
        await m.activate("s1", "prj_missing")
    with pytest.raises(ActivationRefused):
        await m.activate("s1", proj.id, scope="global")


async def test_codex_activation_is_unsupported_by_default(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path)
    await m.install("s1")
    with pytest.raises(ActivationRefused) as ei:
        await m.activate("s1", proj.id, targets=["claude", "codex"])
    assert "unsupported" in ei.value.message and "NOT confirmed" in ei.value.message
    assert not (Path(proj.path) / ".agents").exists()
    # only if the operator explicitly enables it (not confirmed upstream) is the folder written
    m2, repo2, _, proj2 = await setup(h, tmp_path / "x", pname="p2", codex_agents_skills=True)
    await m2.install("s1")
    status, body = await m2.activate("s1", proj2.id, targets=["claude", "codex"])
    await approve_pending(h, body["approval_id"])
    status, _ = await m2.activate("s1", proj2.id, targets=["claude", "codex"])
    assert (Path(proj2.path) / ".agents" / "skills" / "s1" / "SKILL.md").is_file()
    await m2.deactivate("s1", proj2.id)
    assert not (Path(proj2.path) / ".agents").exists()


async def test_catalogue_only_kinds_return_instructions(h, tmp_path):
    ents = [entry("app", kind="standalone_app", install_method={"type": "manual", "command": "brew install app", "notes": "desktop app"}),
            entry("docsx", kind="docs"), entry("coll", kind="collection")]
    repo = tmp_path / "repos" / "app"
    make_repo(repo, {"README.md": "x\n"})
    m = await make_manager(h, tmp_path, {"app": repo, "docsx": repo, "coll": repo})
    await m.import_catalog(write_catalog(tmp_path / "c.yaml", ents))
    proj = await h.project("p1", git_repo=False)
    with pytest.raises(CatalogueOnly) as ei:
        await m.activate("app", proj.id)
    assert ei.value.details["instructions"]["documented_method"]["command"] == "brew install app"
    # a collection without a chosen sub-skill is catalogue-only even after download
    await m.install("coll")
    with pytest.raises(CatalogueOnly):
        await m.activate("coll", proj.id)
    assert not (Path(proj.path) / ".claude").exists()


async def test_collection_with_chosen_subskill_is_activatable(h, tmp_path):
    repo = tmp_path / "repos" / "coll"
    make_repo(repo, {"skills/one/SKILL.md": "---\nname: one\ndescription: d\n---\n", "skills/two/SKILL.md": "---\nname: two\ndescription: d\n---\n"})
    m = await make_manager(h, tmp_path, {"coll": repo})
    await m.import_catalog(write_catalog(tmp_path / "c.yaml", [entry("coll", kind="collection")]))
    proj = await h.project("p1", git_repo=False)
    await m.set_source("coll", "local:coll::skills/two")
    await m.install("coll")
    await activate_ok(h, m, "coll", proj)
    assert (Path(proj.path) / ".claude" / "skills" / "two" / "SKILL.md").is_file()
    assert not (Path(proj.path) / ".claude" / "skills" / "one").exists()


# ---- MCP -------------------------------------------------------------------

MCP_ENTRY = dict(kind="mcp_server", install_method={"type": "mcp_npx", "command": "claude mcp add -e API_KEY=xxx playwright npx @playwright/mcp@latest", "notes": "n"})


async def test_mcp_entry_is_merged_into_project_mcp_json_and_removed(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path, {"README.md": "mcp server\n"}, entries=[entry("s1", **MCP_ENTRY)], platform="win32")
    root = Path(proj.path)
    existing = {"mcpServers": {"mine": {"command": "node", "args": ["mine.js"]}}, "other": 1}
    (root / ".mcp.json").write_text(json.dumps(existing))
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    data = json.loads((root / ".mcp.json").read_text())
    assert data["mcpServers"]["mine"] == existing["mcpServers"]["mine"] and data["other"] == 1
    assert data["mcpServers"]["playwright"] == {"command": "cmd", "args": ["/c", "npx", "@playwright/mcp@latest"], "env": {"API_KEY": "${API_KEY}"}}
    assert "xxx" not in (root / ".mcp.json").read_text()  # placeholders only
    assert not (root / ".claude").exists()
    await m.deactivate("s1", proj.id)
    assert json.loads((root / ".mcp.json").read_text()) == existing


async def test_mcp_json_created_then_deleted_when_we_created_it(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path, {"README.md": "x\n"}, entries=[entry("s1", **MCP_ENTRY)], platform="linux")
    root = Path(proj.path)
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["playwright"]["command"] == "npx"
    await m.deactivate("s1", proj.id)
    assert not (root / ".mcp.json").exists()


async def test_mcp_refuses_existing_name_and_keeps_modified_entry_on_deactivate(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path, {"README.md": "x\n"}, entries=[entry("s1", **MCP_ENTRY)], platform="linux")
    root = Path(proj.path)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "mine"}}}))
    await m.install("s1")
    _, body = await m.activate("s1", proj.id)
    await approve_pending(h, body["approval_id"])
    with pytest.raises(ActivationRefused):
        await m.activate("s1", proj.id)
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["playwright"] == {"command": "mine"}
    (root / ".mcp.json").write_text("{not json")
    with pytest.raises(ActivationRefused):
        await m.activate("s1", proj.id)
    assert (root / ".mcp.json").read_text() == "{not json"


async def test_mcp_entry_modified_by_user_is_left_on_deactivate(h, tmp_path):
    m, repo, sha, proj = await setup(h, tmp_path, {"README.md": "x\n"}, entries=[entry("s1", **MCP_ENTRY)], platform="linux")
    root = Path(proj.path)
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    data = json.loads((root / ".mcp.json").read_text())
    data["mcpServers"]["playwright"]["args"].append("--user-flag")
    (root / ".mcp.json").write_text(json.dumps(data))
    res = await m.deactivate("s1", proj.id)
    assert any("modified since activation" in x for x in res["left"])
    assert "--user-flag" in (root / ".mcp.json").read_text()


async def test_mcp_without_machine_usable_command_is_catalogue_only(h, tmp_path):
    ent = entry("s1", kind="mcp_server", install_method={"type": "mcp_npx", "command": "npx ctx7 setup --claude", "notes": "wizard"})
    m, repo, sha, proj = await setup(h, tmp_path, {"README.md": "x\n"}, entries=[ent])
    await m.install("s1")
    with pytest.raises(CatalogueOnly):
        await m.activate("s1", proj.id)
    assert not (Path(proj.path) / ".mcp.json").exists()


def test_derive_mcp_config_variants():
    from orchestrator.skills.artifacts import derive_mcp_config as d

    assert d({"command": "claude mcp add playwright npx @playwright/mcp@latest"}, "x", platform="linux") == {"name": "playwright", "entry": {"command": "npx", "args": ["@playwright/mcp@latest"]}}
    assert d({"command": "claude mcp add --transport http ctx https://mcp.example.com/mcp"}, "x", platform="linux")["entry"] == {"type": "http", "url": "https://mcp.example.com/mcp"}
    assert d({"command": "claude mcp add s -- node server.js --flag"}, "x", platform="linux")["entry"] == {"command": "node", "args": ["server.js", "--flag"]}
    assert d({"command": "npx ctx7 setup --claude"}, "x") is None and d({"command": None}, "x") is None
    assert d({"mcp": {"name": "n", "command": "uvx", "args": ["p"]}}, "x", platform="win32")["entry"] == {"command": "cmd", "args": ["/c", "uvx", "p"]}


# ---- update / rollback -----------------------------------------------------


async def test_update_then_rollback_restores_previous_ref(h, tmp_path):
    m, repo, sha1, proj = await setup(h, tmp_path)
    root = Path(proj.path)
    await m.install("s1")
    await activate_ok(h, m, "s1", proj)
    sha2 = make_repo(repo, {"SKILL.md": "---\nname: s1\ndescription: v2\n---\nVERSION TWO\n"}, message="v2")
    out = await m.update("s1")
    assert out["ok"] and out["changed"] and out["previous_ref"] == sha1 and out["ref"] == sha2
    s = await m.get_row("s1")
    assert (s["installed_ref"], s["previous_ref"], s["state"]) == (sha2, sha1, "DOWNLOADED_AND_DISABLED")
    assert s["project_scopes"] == [] and not (root / ".claude").exists()  # old copy removed; new ref needs its own approval
    assert (m.skills_dir / "s1" / sha1 / "SKILL.md").is_file() and (m.skills_dir / "s1" / sha2 / "SKILL.md").is_file()
    status, body = await m.activate("s1", proj.id)
    assert status == 202  # new ref -> new approval
    await approve_pending(h, body["approval_id"])
    await m.activate("s1", proj.id)
    assert "VERSION TWO" in (root / ".claude" / "skills" / "s1" / "SKILL.md").read_text()

    rb = await m.rollback("s1")
    assert rb["ok"] and rb["ref"] == sha1 and rb["rolled_back_from"] == sha2 and rb["restored_projects"] == [proj.id]
    s = await m.get_row("s1")
    assert (s["installed_ref"], s["previous_ref"], s["state"]) == (sha1, None, "INSTALLED_AND_ACTIVE")
    text = (root / ".claude" / "skills" / "s1" / "SKILL.md").read_text()
    assert "VERSION TWO" not in text and "Do the thing." in text
    assert json.loads((root / ".claude" / "skills" / "s1" / ".orchestrator-skill.json").read_text())["ref"] == sha1
    assert s["project_scopes"][0]["ref"] == sha1
    with pytest.raises(InvalidSkillState):
        await m.rollback("s1")


async def test_update_same_ref_is_noop_and_failed_update_keeps_state(h, tmp_path):
    m, repo, sha1, proj = await setup(h, tmp_path)
    await m.install("s1")
    out = await m.update("s1")
    assert out["ok"] and out["changed"] is False
    out = await m.update("s1", ref="0" * 40)
    assert out["ok"] is False and out["retryable"]
    s = await m.get_row("s1")
    assert s["state"] == "DOWNLOADED_AND_DISABLED" and s["installed_ref"] == sha1 and s["previous_ref"] is None
    last = (await h.ctx.db.all("SELECT * FROM skill_events WHERE skill_id='s1' ORDER BY ts DESC LIMIT 1"))[0]
    assert last["action"] == "update_failed"


async def test_update_back_to_review_when_new_version_adds_scripts(h, tmp_path):
    m, repo, sha1, proj = await setup(h, tmp_path)
    await m.install("s1")
    assert (await m.get_row("s1"))["state"] == "DOWNLOADED_AND_DISABLED"
    make_repo(repo, {"scripts/new.sh": "echo\n"}, message="adds a script")
    out = await m.update("s1")
    assert out["state"] == "NEEDS_SECURITY_REVIEW"
    s = await m.get_row("s1")
    assert s["state"] == "NEEDS_SECURITY_REVIEW" and s["previous_ref"] == sha1
    rb = await m.rollback("s1")
    assert (await m.get_row("s1"))["state"] == "DOWNLOADED_AND_DISABLED" and rb["ref"] == sha1


async def test_rollback_skips_projects_without_prior_approval(h, tmp_path):
    m, repo, sha1, proj = await setup(h, tmp_path)
    await m.install("s1")
    make_repo(repo, {"extra.md": "e\n"}, message="v2")
    await m.update("s1")
    rb = await m.rollback("s1")
    assert rb["restored_projects"] == [] and rb["skipped"] == []  # nothing was active before the update
    assert (await m.get_row("s1"))["installed_ref"] == sha1
