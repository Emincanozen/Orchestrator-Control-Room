import collections

import pytest
from skills_helpers import entry, make_manager, make_repo, simple_skill_files, write_catalog

from orchestrator.config import CONFIG_DIR
from orchestrator.skills import store
from orchestrator.skills.catalog import load_catalog
from orchestrator.skills.errors import CatalogError
from orchestrator.skills.sources import GithubSourcePolicy, normalize_source
from orchestrator.testing import make_harness


@pytest.fixture
async def h(tmp_path):
    harness = await make_harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


REAL = CONFIG_DIR / "skills-catalog.yaml"


async def test_real_catalog_import(h, tmp_path):
    from orchestrator.skills.manager import SkillManager

    m = SkillManager(h.ctx, h.approvals)  # default GitHub-only policy, as in production
    rep = await m.import_catalog(REAL)
    assert (rep.total, rep.created, rep.updated, rep.unchanged) == (41, 41, 0, 0)
    rows = await m.all_rows()
    assert len(rows) == 41
    assert {r["state"] for r in rows} == {"CATALOGUED_ON_DEMAND"}
    assert collections.Counter(r["kind"] for r in rows) == {"agent_skill": 21, "dev_tool": 7, "collection": 5, "mcp_server": 4, "standalone_app": 2, "docs": 2}
    not_inst = {r["id"]: m.installable(r) for r in rows if not m.installable(r)[0]}
    assert len(not_inst) == 8 and len(rep.not_installable) == 8
    assert set(not_inst) == {"voice-dna", "agent-skills", "skill-time-machine", "automaton", "chief-of-staff", "teach", "claudex-loop", "awesome-skills"}
    for ok, reason in not_inst.values():
        assert "source URL unknown" in reason
    # a null-source entry keeps its uncertainty and stays catalogue-only with a clear reason
    by_id = {r["id"]: r for r in rows}
    assert by_id["voice-dna"]["source_url"] is None and by_id["voice-dna"]["uncertainty"]
    assert by_id["remotion"]["install_method"]["catalog"]["candidates"] == ["https://github.com/remotion-dev/remotion"]
    assert by_id["context7"]["pinned_ref"] == "681554c5d3a688c67879d02d4f01d79ffd15df13"
    # sub-path sources are real GitHub URLs under the default policy
    spec = GithubSourcePolicy().parse(by_id["find-skills"]["source_url"])
    assert spec.subpath == "skills/find-skills" and spec.ref == "main"
    # every skill_event / state is untouched by import (no events)
    assert await h.ctx.db.all("SELECT id FROM skill_events") == []


async def test_reimport_is_idempotent_and_preserves_local_state(h, tmp_path):
    from orchestrator.skills.manager import SkillManager

    m = SkillManager(h.ctx, h.approvals)
    await m.import_catalog(REAL)
    # simulate local progress on one skill, plus a user-chosen source on an ambiguous entry
    def progress(tx):
        cur = store.get_tx(tx, "remotion")
        store.change_tx(h.ctx, tx, "remotion", "download", to_state="DOWNLOADED_AND_DISABLED",
                        fields={"installed_ref": "a" * 40, "local_path": "x", "security": {**cur["security"], "inspected": True, "risk": "low"}},
                        ref="a" * 40)
    await h.ctx.db.write(progress)
    await m.set_source("voice-dna", "https://github.com/someone/voice-dna")
    before = {r["id"]: r for r in await m.all_rows()}
    rep = await m.import_catalog(REAL)
    assert (rep.created, rep.updated, rep.unchanged) == (0, 0, 41)
    after = {r["id"]: r for r in await m.all_rows()}
    assert after["remotion"]["state"] == "DOWNLOADED_AND_DISABLED" and after["remotion"]["installed_ref"] == "a" * 40
    assert after["remotion"]["security"]["inspected"] is True
    assert after["voice-dna"]["source_url"] == "https://github.com/someone/voice-dna"
    assert after["voice-dna"]["source_verified"] is False and after["voice-dna"]["install_method"]["local"]["source"] == "user"
    assert {k: v["updated_at"] for k, v in before.items()} == {k: v["updated_at"] for k, v in after.items()}


async def test_reimport_refreshes_catalog_fields_only(h, tmp_path):
    from orchestrator.skills.manager import SkillManager

    m = SkillManager(h.ctx, h.approvals)
    cat = write_catalog(tmp_path / "c.yaml", [entry("a", source_url="https://github.com/o/a")])
    await m.import_catalog(cat)
    write_catalog(cat, [entry("a", source_url="https://github.com/o/a", description="changed text")])
    rep = await m.import_catalog(cat)
    assert rep.updated == 1
    assert (await m.get_row("a"))["description"] == "changed text"
    assert (await m.get_row("a"))["state"] == "CATALOGUED_ON_DEMAND"


async def test_invalid_catalog_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    write_catalog(bad, [entry("a", kind="nonsense"), entry("a")])
    with pytest.raises(CatalogError) as ei:
        load_catalog(bad)
    assert ei.value.problems
    dup = tmp_path / "dup.yaml"
    write_catalog(dup, [entry("a"), entry("a")])
    with pytest.raises(CatalogError):
        load_catalog(dup)
    ref = tmp_path / "ref.yaml"
    write_catalog(ref, [entry("a", duplicates_of="ghost")])
    with pytest.raises(CatalogError):
        load_catalog(ref)


def test_normalize_source():
    assert normalize_source("https://github.com/Foo/Bar.git") == normalize_source("http://www.github.com/foo/bar/") == "github.com/foo/bar"
    assert normalize_source("https://github.com/o/r/tree/main/skills/x") == "github.com/o/r#skills/x"
    assert normalize_source(None) is None


async def test_duplicate_and_conflict_warnings(h, tmp_path):
    from orchestrator.skills.manager import SkillManager

    m = SkillManager(h.ctx, h.approvals)
    cat = write_catalog(tmp_path / "c.yaml", [
        entry("one", source_url="https://github.com/o/shared"),
        entry("two", source_url="https://github.com/O/shared.git/"),  # same normalised URL
        entry("three", source_url="https://github.com/p/other", duplicates_of="one"),
        entry("sk-a", source_url="https://github.com/a/repo-a/tree/main/skills/same-name"),
        entry("sk-b", source_url="https://github.com/b/repo-b/tree/main/skills/same-name"),  # same skill folder name
        entry("mcp-a", kind="mcp_server", source_url="https://github.com/a/tool"),
        entry("mcp-b", kind="mcp_server", source_url="https://github.com/b/tool"),
        entry("solo", source_url="https://github.com/c/solo"),
    ])
    rep = await m.import_catalog(cat)
    w = rep.warnings
    assert {x["reason"] for x in w["one"] if x["type"] == "duplicate"} >= {"same_source_url", "duplicated_by"}
    assert w["two"][0]["type"] == "duplicate" and w["two"][0]["with"] == ["one"]
    assert any(x["reason"] == "duplicates_of" for x in w["three"])
    assert [x for x in w["sk-a"] if x["type"] == "conflict"][0]["with"] == ["sk-b"]
    assert [x for x in w["mcp-a"] if x["type"] == "conflict"][0]["name"] == "tool"
    assert "solo" not in w
    # the API view carries them too
    rows = await m.all_rows()
    assert m.warnings_for(rows)["sk-b"][0]["type"] == "conflict"


async def test_real_catalog_warnings_are_computed(h):
    from orchestrator.skills.manager import SkillManager

    m = SkillManager(h.ctx, h.approvals)
    rep = await m.import_catalog(REAL)
    # the analysis runs over the real catalog without error and only reports known warning types
    assert all(x["type"] in ("duplicate", "conflict") for ws in rep.warnings.values() for x in ws)
