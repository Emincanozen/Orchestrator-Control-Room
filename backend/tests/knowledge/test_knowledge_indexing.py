import json
import os

import pytest

import kn_seed as S
from orchestrator.knowledge import retrieval, sources
from orchestrator.knowledge.indexer import Indexer
from orchestrator.knowledge.sources import GlobError, normalise_error

SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@pytest.fixture
def ctx(tmp_path):
    c = S.open_ctx(tmp_path)
    yield c
    c.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    S.make_repo(root, {
        ".gitignore": "docs/private.md\nnode_modules/\n",
        "README.md": "# Demo\n\nA demo project about quantum widgets and flux capacitors.\n",
        "CLAUDE.md": "# Rules\n\nAlways run the tests before committing.\n",
        "docs/guide.md": "# Guide\n\n## Install\n\nRun the installer to set up the zebra service.\n\n## Usage\n\nCall the zebra endpoint.\n",
        "docs/adr/0001-use-sqlite.md": "# 1. Use SQLite\n\nWe decided to use SQLite for storage because it is local-first.\n",
        "docs/DECISIONS.md": "# Decisions\n\nIntro text.\n\n## ADR-007 Deterministic verification\n\nCompletion needs a passing check, no model opinion.\n\n## Notes\n\nOther.\n",
        "docs/requirements.md": "# Requirements\n\nThe system shall export reports as CSV.\n",
        "docs/notes.md": f"# Notes\n\nDeploy with api_key = {SECRET} and keep going.\n",
        "docs/private.md": "# Private\n\nIgnored by git.\n",
        "docs/secrets.md": "# Secrets\n\nlooks secret by name\n",
        "docs/blob.md": b"# Blob\n\x00\x01\x02binary payload",
        "docs/big.md": "# Big\n\n" + ("lorem ipsum " * 20_000),
        "docs/key.md": "# Key\n\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n",
        "docs/image.png": b"\x89PNG\r\n\x1a\n\x00\x00",
        "src/app.py": "SECRET_TOKEN_NAME = 'x'\n\nclass Widget:\n    def render(self):\n        return 'hidden-body-literal-12345'\n\ndef build():\n    pass\n\nasync def serve():\n    pass\n",
        "src/ui.ts": "export function mount() {}\nexport class Panel {}\nexport const VERSION = 1;\n",
        "src/.env": "TOKEN=abc",
    })
    S.write(root, {"docs/untracked.md": "# Untracked\n\nBut not ignored: pelican section.\n"})
    return root


async def run(ctx, pid, **kw):
    return await Indexer(ctx.db).run(project_id=pid, **kw)


def docs(ctx, **where):
    q = "SELECT * FROM knowledge_docs"
    if where:
        q += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return ctx.db.read_sync(lambda tx: [dict(r) for r in tx.all(q + " ORDER BY source_path, id", tuple(where.values()))])


async def test_incremental_unchanged_changed_deleted(ctx, repo):
    pid = S.project(ctx, str(repo))
    r1 = await run(ctx, pid, sources=["docs"])
    assert r1.added > 0 and r1.updated == 0 and r1.removed == 0 and not r1.errors
    before = {d["id"]: (d["source_hash"], d["updated_at"]) for d in docs(ctx)}
    r2 = await run(ctx, pid, sources=["docs"])
    assert (r2.added, r2.updated, r2.removed) == (0, 0, 0) and r2.skipped == r1.added
    assert {d["id"]: (d["source_hash"], d["updated_at"]) for d in docs(ctx)} == before  # untouched rows
    (repo / "docs" / "guide.md").write_text("# Guide\n\n## Install\n\nRun the installer to set up the giraffe service.\n", encoding="utf-8")
    r3 = await run(ctx, pid, sources=["docs"])
    assert (r3.added, r3.updated, r3.removed) == (0, 1, 0)
    assert any("giraffe" in d["body"] for d in docs(ctx, source_path="docs/guide.md"))
    assert not any("zebra" in d["body"] for d in docs(ctx, source_path="docs/guide.md"))
    (repo / "docs" / "untracked.md").unlink()
    r4 = await run(ctx, pid, sources=["docs"])
    assert (r4.added, r4.updated, r4.removed) == (0, 0, 1) and not docs(ctx, source_path="docs/untracked.md")
    hit = ctx.db.read_sync(lambda tx: retrieval.fts_search(tx, "zebra"))
    assert hit == []  # FTS index followed the update/delete triggers
    r5 = await run(ctx, pid, sources=["docs"], full=True)
    assert r5.updated > 0  # full rebuild re-chunks everything


async def test_policy_skips(ctx, repo):
    pid = S.project(ctx, str(repo))
    r = await run(ctx, pid, sources=["docs"], globs=["README*", "docs/**/*", "CLAUDE.md"])
    paths = {d["source_path"] for d in docs(ctx)}
    assert {"README.md", "CLAUDE.md", "docs/guide.md", "docs/untracked.md"} <= paths
    skipped = {d["path"]: d["reason"] for d in r.skipped_details}
    assert "docs/private.md" not in paths and "docs/private.md" not in skipped  # gitignored: never even listed
    assert "docs/secrets.md" not in paths and "secret" in skipped["docs/secrets.md"]
    assert "docs/blob.md" not in paths and skipped["docs/blob.md"] == "binary file"
    assert "docs/big.md" not in paths and "larger than" in skipped["docs/big.md"]
    assert "docs/key.md" not in paths and skipped["docs/key.md"] == "contains a private key"
    assert "docs/image.png" not in paths and skipped["docs/image.png"] == "not a text document"


async def test_secrets_redacted_before_storage(ctx, repo):
    pid = S.project(ctx, str(repo))
    await run(ctx, pid, sources=["docs"])
    blob = json.dumps(docs(ctx))
    assert SECRET not in blob and "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in blob
    assert "[REDACTED]" in "".join(d["body"] for d in docs(ctx, source_path="docs/notes.md"))
    assert ctx.db.read_sync(lambda tx: retrieval.fts_search(tx, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")) == []


async def test_kinds_and_provenance(ctx, repo):
    pid = S.project(ctx, str(repo))
    await run(ctx, pid, sources=["docs"])
    kinds = {(d["source_path"], d["kind"]) for d in docs(ctx)}
    assert ("docs/adr/0001-use-sqlite.md", "adr") in kinds
    assert ("docs/DECISIONS.md", "adr") in kinds  # the ADR-007 section
    assert ("docs/requirements.md", "requirement") in kinds
    assert ("README.md", "doc") in kinds
    adr_chunks = docs(ctx, source_path="docs/DECISIONS.md")
    assert {d["kind"] for d in adr_chunks} <= {"adr", "doc"} and any(d["kind"] == "adr" and "ADR-007" in d["title"] for d in adr_chunks)
    hits = ctx.db.read_sync(lambda tx: retrieval.fts_search(tx, "pelican", project_id=pid))
    assert hits and set(hits[0].as_dict()) == {"doc_id", "kind", "title", "source_path", "project_id", "updated_at", "score", "snippet"}
    h = hits[0].as_dict()
    assert h["source_path"] == "docs/untracked.md" and h["project_id"] == pid and h["doc_id"].startswith("kd_") and "«pelican»" in h["snippet"]


async def test_path_traversal_refused(ctx, repo, tmp_path):
    for bad in ("../*.md", "docs/../../x.md", "/etc/*.md", "C:/Windows/*.md", "..\\x.md"):
        with pytest.raises(GlobError):
            sources.validate_globs([bad])
    pid = S.project(ctx, str(repo))
    with pytest.raises(GlobError):
        await run(ctx, pid, globs=["../*.md"])
    (tmp_path / "outside.md").write_text("# Outside\n\nshould never be indexed outsideword\n", encoding="utf-8")
    assert sources._safe_file(repo, "../outside.md") is None  # path policy on the file level
    link = repo / "docs" / "link.md"
    try:
        os.symlink(tmp_path / "outside.md", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    await run(ctx, pid, sources=["docs"], globs=["docs/**/*.md"])
    assert "docs/link.md" not in {d["source_path"] for d in docs(ctx)}
    assert not ctx.db.read_sync(lambda tx: retrieval.fts_search(tx, "outsideword"))


async def test_non_git_project_walks_and_skips_vendor(ctx, tmp_path):
    root = tmp_path / "plain"
    S.write(root, {"README.md": "# Plain\n\nhello plainword\n", "node_modules/x/README.md": "# vendor\n\nvendorword\n", "docs/a.md": "# A\n\nalpha text\n"})
    pid = S.project(ctx, str(root))
    r = await run(ctx, pid)
    assert {d["source_path"] for d in docs(ctx)} == {"README.md", "docs/a.md"} and not r.errors
    r = await run(ctx, pid, sources=["code"])
    assert docs(ctx, kind="code_meta") == [] and any("not a git repository" in d["reason"] for d in r.skipped_details)


async def test_code_metadata_only_tracked_and_no_content_dump(ctx, repo):
    pid = S.project(ctx, str(repo))
    S.write(repo, {"src/untracked.py": "def sneaky():\n    pass\n"})
    r = await run(ctx, pid, sources=["code"])
    by = {d["source_path"]: d for d in docs(ctx, kind="code_meta")}
    assert set(by) == {"src/app.py", "src/ui.ts"}  # tracked + source languages only; .env and untracked excluded
    body = by["src/app.py"]["body"]
    assert "language: python" in body and "class Widget" in body and "def build" in body and "async def serve" in body and "var SECRET_TOKEN_NAME" in body
    assert "hidden-body-literal-12345" not in body and "return" not in body  # metadata, not a dump
    assert "function mount" in by["src/ui.ts"]["body"] and "class Panel" in by["src/ui.ts"]["body"]
    assert "src/.env" not in by and r.errors == []


def finished_task(ctx, pid, **kw):
    ac = kw.pop("acceptance_criteria", ["login returns 200"])
    return S.task(ctx, pid, acceptance_criteria=json.dumps(ac), relevant_files=json.dumps(["src/auth.py"]), **kw)


async def test_task_summaries_are_compact_and_structured(ctx, repo):
    pid = S.project(ctx, str(repo))
    t = finished_task(ctx, pid, title="Fix login timeout", description="Users time out after 30s", active=42, tid="tsk_a")
    S.run(ctx, t, provider="claude", model="opus", usage={"input_tokens": 5}, status="succeeded")
    S.verification(ctx, t, "passed", "2026-03-01T09:30:00+00:00")
    S.task(ctx, pid, state="RUNNING", title="still running", tid="tsk_b")
    r = await run(ctx, pid, sources=["tasks"])
    assert r.added == 1
    (d,) = docs(ctx, kind="task")
    assert d["source_path"] == "task:tsk_a" and d["project_id"] == pid
    for needle in ("Objective: Fix login timeout", "Acceptance criteria: login returns 200", "Outcome: COMPLETED", "Verification: passed",
                   "src/auth.py", "Provider/model: claude/opus", "Duration: 42"):
        assert needle in d["body"], needle
    assert len(d["body"]) < 1500 and d["updated_at"] == "2026-03-01T10:00:00+00:00"
    assert (await run(ctx, pid, sources=["tasks"])).skipped == 1


def error_story(ctx, pid, *, repo=None, refs=None, number="500", path="C:\\proj\\src\\auth.py"):
    t = S.task(ctx, pid, title="Fix login", tid="tsk_err", relevant_files=json.dumps(["src/auth.py"]), finished="2026-03-02T10:00:00+00:00")
    S.run(ctx, t, provider="claude", model="opus", status="failed", start="2026-03-02T09:00:00+00:00", end="2026-03-02T09:05:00+00:00",
          category="test_failure", message=f"AssertionError at {path} line 42: expected 200 got {number}")
    S.verification(ctx, t, "failed", "2026-03-02T09:06:00+00:00", checks=[
        {"name": "pytest", "kind": "command", "passed": False, "exit_code": 1, "output": f"...\nFAILED tests/test_auth.py::test_login - assert {number} == 200"}])
    S.run(ctx, t, provider="codex", model="gpt-x", phase="repair", status="succeeded", start="2026-03-02T09:20:00+00:00", end="2026-03-02T09:30:00+00:00")
    S.verification(ctx, t, "passed", "2026-03-02T09:40:00+00:00")
    for ref, at in (refs or []):
        S.sql(ctx, "INSERT INTO checkpoints (id, task_id, kind, summary, git_ref, created_at) VALUES (?,?,?,?,?,?)", (S.uid("ckp"), t, "auto", "{}", ref, at))
    return t


def test_error_signature_normalisation():
    a = normalise_error("FAILED tests/test_auth.py::test_login - assert 500 == 200")
    b = normalise_error("FAILED  tests\\test_auth.py::test_login - assert 404 == 200")
    assert a == b == "failed <path>::test_login - assert N == N"
    assert normalise_error("Error at C:\\Users\\me\\x\\a.py line 12: 0xDEADBEEF tsk_01ABCDEFGHJKMNPQRSTVWXYZ00") == "error at <path> line N: <hex> <id>"
    assert normalise_error("") == ""


async def test_error_resolution_from_failed_then_passed_history(ctx, repo):
    pid = S.project(ctx, str(repo))
    c1 = S.git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("def build():\n    return 1\n", encoding="utf-8")
    S.git(repo, "add", "-A")
    S.git(repo, "commit", "-q", "-m", "fix")
    c2 = S.git(repo, "rev-parse", "HEAD")
    error_story(ctx, pid, refs=[(c1, "2026-03-02T09:01:00+00:00"), (c2, "2026-03-02T09:35:00+00:00")])
    S.task(ctx, pid, title="never fixed", tid="tsk_nf")
    S.run(ctx, "tsk_nf", status="failed", start="2026-03-02T08:00:00+00:00", category="x")  # failed, never passed: no resolution
    r = await run(ctx, pid, sources=["errors"])
    assert r.added == 1
    (d,) = docs(ctx, kind="error_resolution")
    assert d["source_path"] == "error:tsk_err"
    b = d["body"]
    assert "Error signature: pytest: failed <path>::test_login - assert N == N" in b
    assert "Category: test_failure" in b
    assert "failing claude/opus -> passing codex/gpt-x" in b
    assert "git diff between checkpoints" in b and "src/app.py" in b and "Diff stat:" in b  # what changed, from real git
    assert "C:\\proj" not in d["title"] and "500" not in d["title"]
    assert (await run(ctx, pid, sources=["errors"])).skipped == 1


async def test_error_resolution_falls_back_to_relevant_files_without_git_refs(ctx, repo):
    pid = S.project(ctx, str(repo))
    error_story(ctx, pid)
    await run(ctx, pid, sources=["errors"])
    (d,) = docs(ctx, kind="error_resolution")
    assert "task relevant_files (git diff unavailable)" in d["body"] and "src/auth.py" in d["body"]


async def test_skills_decisions_models_sources(ctx, repo):
    pid = S.project(ctx, str(repo))
    S.sql(ctx, "INSERT INTO skills (id, name, category, kind, description, state, updated_at) VALUES ('sk1','Pdf tool','docs','agent_skill','Extracts tables from pdf files','DOWNLOADED_AND_DISABLED','2026-03-01T00:00:00+00:00')")
    S.sql(ctx, "INSERT INTO decisions (id, project_id, title, context, decision, rationale, created_at) VALUES ('dc1',?,?,?,?,?,?)",
          (pid, "Choose queue", "need a job queue", "use sqlite table", "no extra service", "2026-03-01T00:00:00+00:00"))
    t = S.task(ctx, pid)
    S.run(ctx, t, provider="claude", model="m1", start="2026-03-01T09:00:00+00:00")
    S.verification(ctx, t, "passed", "2026-03-01T09:30:00+00:00")
    r = await Indexer(ctx.db).run(sources=["skills", "decisions", "models"])
    assert r.added == 3 and not r.errors
    assert docs(ctx, kind="skill")[0]["project_id"] is None and "Extracts tables" in docs(ctx, kind="skill")[0]["body"]
    dec = docs(ctx, source_path="decision:dc1")[0]
    assert dec["kind"] == "adr" and dec["project_id"] == pid and "no extra service" in dec["body"]
    assert "1 of 1 verified runs" in docs(ctx, kind="model_history")[0]["body"]
    S.sql(ctx, "DELETE FROM skills WHERE id = 'sk1'")
    assert (await Indexer(ctx.db).run(sources=["skills"])).removed == 1


async def test_deleted_project_docs_are_dropped(ctx, repo):
    pid = S.project(ctx, str(repo))
    await run(ctx, pid, sources=["docs"])
    assert docs(ctx)
    S.sql(ctx, "DELETE FROM projects WHERE id = ?", (pid,))
    r = await Indexer(ctx.db).run(sources=["docs"])
    assert r.removed > 0 and docs(ctx) == []


async def test_memory_separation_index_never_writes_task_state(ctx, repo):
    pid = S.project(ctx, str(repo))
    error_story(ctx, pid)
    snap = lambda: ctx.db.read_sync(lambda tx: [tuple(r) for t in ("tasks", "runs", "verifications", "checkpoints", "state_transitions")  # noqa: E731
                                              for r in tx.all(f"SELECT * FROM {t} ORDER BY 1")])
    before = snap()
    await run(ctx, pid)
    assert snap() == before


def test_source_scan_no_writes_outside_knowledge_tables():
    import re
    from pathlib import Path

    import orchestrator.knowledge as pkg

    bad = re.compile(r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+(?!knowledge_)(\w+)", re.I)
    for f in Path(pkg.__file__).parent.glob("*.py"):
        for m in bad.finditer(f.read_text(encoding="utf-8")):
            raise AssertionError(f"{f.name} writes to a non-knowledge table: {m.group(0)}")
    assert "never task state" in pkg.__doc__ or "never task state" in " ".join(pkg.__doc__.split())
