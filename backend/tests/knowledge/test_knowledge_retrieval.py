import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import kn_seed as S
import orchestrator.knowledge as knowledge_pkg
from orchestrator.api.app import create_app
from orchestrator.api.routes import knowledge as knowledge_routes
from orchestrator.domain import events as ev
from orchestrator.knowledge import retrieval
from orchestrator.knowledge.embedding import HashingEmbedder, cosine
from orchestrator.knowledge.indexer import Indexer
from orchestrator.knowledge.service import KnowledgeService

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def ctx(tmp_path):
    c = S.open_ctx(tmp_path)
    yield c
    c.close()


def put(ctx, did, kind, title, body, *, project="p1", path=None, at="2026-11-01T00:00:00+00:00"):
    S.sql(ctx, "INSERT INTO knowledge_docs (id, project_id, kind, title, body, source_path, source_hash, updated_at) VALUES (?,?,?,?,?,?,?,?)",
          (did, project, kind, title, body, path or did, "h", at))


def search(ctx, q, **kw):
    return ctx.db.read_sync(lambda tx: retrieval.fts_search(tx, q, **kw))


HOSTILE = ['"unbalanced', "foo OR", "NEAR(a b)", "NEAR(alpha beta, 2)", "title:secret", "body: sqlite", "* ", "^start", "a AND NOT b", ")(", "alpha*", "{col}:x",
           "'; DROP TABLE knowledge_docs; --", "-minus +plus", "\"\"", "NOT", "AND OR NOT", "title : (alpha OR beta)", "\x00", "ünï cödé"]


def test_fts_query_injection_safety(ctx):
    put(ctx, "d1", "doc", "Alpha storage", "alpha beta sqlite storage notes")
    for q in HOSTILE:
        search(ctx, q)  # must never raise an FTS5 syntax error
    assert retrieval.build_fts_query("   !!! ") is None and search(ctx, "   !!! ") == []
    assert retrieval.build_fts_query('x" OR "y') == '"x" OR "or" OR "y"'  # operators and quotes become inert literals
    assert retrieval.build_fts_query("NEAR(a b)") == '"near" OR "b"' or '"near"' in retrieval.build_fts_query("NEAR(a b)")
    # a column filter in user text is just words: it must not restrict matching to the title column
    assert [h.doc_id for h in search(ctx, "body: sqlite")] == ["d1"]
    assert [h.doc_id for h in search(ctx, "title:sqlite")] == ["d1"]  # 'sqlite' is only in the body
    assert ctx.db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_docs")) == 1


def test_ranking_sanity(ctx):
    put(ctx, "body_only", "doc", "General", "the scheduler picks tasks from the queue")
    put(ctx, "title_hit", "doc", "Scheduler design", "how things run")
    put(ctx, "both", "doc", "Scheduler queue", "scheduler queue scheduler queue details")
    put(ctx, "unrelated", "doc", "Billing", "invoices and payments")
    hits = search(ctx, "scheduler queue")
    ids = [h.doc_id for h in hits]
    assert ids[0] == "both" and "unrelated" not in ids and set(ids) == {"both", "title_hit", "body_only"}
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))
    # kind weighting: an ADR beats an equally relevant skill
    put(ctx, "adr1", "adr", "Retry policy", "retry policy exponential backoff")
    put(ctx, "skill1", "skill", "Retry policy", "retry policy exponential backoff")
    k = [h.doc_id for h in search(ctx, "retry policy backoff")]
    assert k.index("adr1") < k.index("skill1")
    # recency: newer doc wins at equal relevance
    put(ctx, "old", "doc", "Cache note", "cache invalidation rules", at="2020-01-01T00:00:00+00:00")
    put(ctx, "new", "doc", "Cache note", "cache invalidation rules", at="2026-11-10T00:00:00+00:00")
    r = [h.doc_id for h in search(ctx, "cache invalidation")]
    assert r.index("new") < r.index("old")
    # filters
    assert {h.doc_id for h in search(ctx, "retry policy", kinds=["skill"])} == {"skill1"}
    put(ctx, "other", "doc", "Retry policy", "retry policy elsewhere", project="p2")
    assert "other" not in {h.doc_id for h in search(ctx, "retry policy", project_id="p1")}
    assert search(ctx, "retry policy", kinds=["bogus"]) == []


def test_exact_facts_come_from_sql(ctx):
    p = S.project(ctx, "/tmp/p")
    t = S.task(ctx, p, tid="tsk_x", title="Exact")
    assert retrieval.task_facts(ctx.db._reader and __import__("orchestrator.db.database", fromlist=["Tx"]).Tx(ctx.db._reader, writable=False), "tsk_x")["title"] == "Exact"
    assert ctx.db.read_sync(lambda tx: retrieval.task_facts(tx, "nope")) is None
    S.run(ctx, t, start="2026-03-01T09:00:00+00:00")
    S.verification(ctx, t, "passed", "2026-03-01T10:00:00+00:00")
    st = ctx.db.read_sync(lambda tx: retrieval.model_stats(tx))
    assert st == [{"provider": "claude", "model": "m1", "task_type": "coding", "verified_runs": 1, "successes": 1}]


def test_context_for_task_budget_labels_and_relevance(ctx):
    for i in range(6):
        put(ctx, f"doc{i}", "doc", f"Login flow {i}", "login session timeout handling in the auth module " * 40, path=f"docs/login{i}.md")
    put(ctx, "irrelevant", "doc", "Billing", "invoices and payments " * 30)
    put(ctx, "pasttask", "task", "Fix login timeout", "Objective: fix login session timeout\nOutcome: COMPLETED", path="task:old")
    put(ctx, "own", "task", "Fix login timeout again", "login session timeout own summary", path="task:tsk_me")
    put(ctx, "err", "error_resolution", "Error resolution: boom", "Error signature: connection reset by peer N\nWhat changed: retry added", path="error:tsk_o")
    task = {"id": "tsk_me", "project_id": "p1", "title": "Fix login session timeout", "description": "users get logged out", "acceptance_criteria": ["login stays valid"]}
    for budget in (400, 1000, 4000):
        sn = ctx.db.read_sync(lambda tx: retrieval.context_for_task(tx, task, budget_chars=budget, error_signature="Connection reset by peer 104"))
        assert sum(len(s.render()) for s in sn) <= budget
    sn = ctx.db.read_sync(lambda tx: retrieval.context_for_task(tx, task, budget_chars=4000, error_signature="Connection reset by peer 104"))
    ids = [s.doc_id for s in sn]
    assert ids[0] == "err" and "irrelevant" not in ids and "own" not in ids and "pasttask" in ids
    assert all(retrieval.UNTRUSTED_LABEL in s.render() and "source=" in s.render() for s in sn)
    assert all(s.as_dict()["label"] == retrieval.UNTRUSTED_LABEL for s in sn)
    assert "untrusted" in retrieval.render_context(sn).lower()
    assert ctx.db.read_sync(lambda tx: retrieval.context_for_task(tx, task, budget_chars=100)) == []
    assert ctx.db.read_sync(lambda tx: retrieval.context_for_task(tx, {"id": "z", "project_id": "p1", "title": "zzzqqq xxxyyy"}, budget_chars=4000)) == []


def test_hashing_embedder_is_lexical_and_labelled():
    e = HashingEmbedder()
    assert e.semantic is False and "not semantic" in e.description
    a, b, c = e.embed("database connection pool"), e.embed("database connection pool timeout"), e.embed("invoice payment")
    assert cosine(a, b) > 0.5 > cosine(a, c) and abs(cosine(a, a) - 1) < 1e-6
    assert cosine(e.embed("car"), e.embed("automobile")) < 0.2  # synonyms are NOT recognised: no semantic claim


async def test_vectors_off_by_default_then_hybrid_when_enabled(ctx):
    put(ctx, "a", "doc", "Pool", "database connection pool exhaustion", project=None)
    put(ctx, "b", "task", "Pool task", "database connection pool tool", project=None)
    svc = KnowledgeService(ctx)
    await svc.index(sources=["skills"])  # nothing to embed by default
    assert ctx.db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_vectors")) == 0
    r = await svc.search("database pool")
    assert r["used"] == {"fts": True, "vectors": False} and r["results"]
    S.sql(ctx, "INSERT INTO settings (key, value, updated_at) VALUES ('knowledge', ?, 'x')", (json.dumps({"vectors": {"kinds": ["doc"]}}),))
    await svc.index(sources=["skills"])
    rows = ctx.db.read_sync(lambda tx: [r["doc_id"] for r in tx.all("SELECT doc_id FROM knowledge_vectors")])
    assert rows == ["a"]  # only the explicitly enabled kind
    r = await svc.search("database pool")
    assert r["used"] == {"fts": True, "vectors": True} and {x["doc_id"] for x in r["results"]} >= {"a", "b"}
    r = await svc.search("database pool", kinds=["task"])
    assert r["used"]["vectors"] is False  # enabled kinds not in the filter
    S.sql(ctx, "DELETE FROM settings WHERE key = 'knowledge'")
    await svc.index(sources=["skills"])
    assert ctx.db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_vectors")) == 0
    st = await svc.stats()
    assert st["vector_search"]["semantic"] is False and st["vector_search"]["enabled"] is False


async def test_event_driven_indexing_on_task_completion(ctx, tmp_path):
    root = tmp_path / "pr"
    root.mkdir()
    pid = S.project(ctx, str(root))
    svc = KnowledgeService(ctx)
    w = asyncio.create_task(svc.watch_events())
    try:
        await asyncio.sleep(0.05)
        t = S.task(ctx, pid, title="Refactor zeppelin module", tid="tsk_ev")
        await ctx.bus.emit(ev.TASK_STATE_CHANGED, task_id=t, project_id=pid, payload={"from": "TESTING", "to": "COMPLETED"})
        for _ in range(60):
            await asyncio.sleep(0.05)
            if ctx.db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_docs WHERE source_path = 'task:tsk_ev'")):
                break
        assert ctx.db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_docs WHERE source_path = 'task:tsk_ev'")) == 1
    finally:
        w.cancel()


# ---- API --------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={}, start_scheduler=False, extra_routers=[knowledge_routes.router],
                     on_startup=[knowledge_pkg.make_startup(watch=False)])
    with TestClient(app, base_url=BASE) as c:
        c.rt = app.state.rt
        c.app_ = app
        c.tmp = tmp_path
        c.headers.update({"Authorization": f"Bearer {app.state.rt.token}"})
        yield c


def test_api_auth(client):
    anon = TestClient(client.app_, base_url=BASE)
    assert anon.post("/api/knowledge/search", json={"query": "x"}).status_code == 401
    assert anon.post("/api/knowledge/index", json={}).status_code == 401
    for p in ("/docs", "/docs/x", "/stats"):
        assert anon.get("/api/knowledge" + p).status_code == 401
    assert anon.delete("/api/knowledge/docs/x").status_code == 401


def test_api_flow(client):
    root = client.tmp / "repo"
    S.make_repo(root, {"README.md": "# Demo\n\nThe flux capacitor needs 1.21 gigawatts.\n", "docs/a.md": "# A\n\nplain text about tulips\n"})
    pid = client.post("/api/projects", json={"name": "demo", "path": str(root)}).json()["id"]
    rep = client.post("/api/knowledge/index", json={"project_id": pid, "sources": ["docs"], "wait": True}).json()
    assert set(rep) == {"added", "updated", "removed", "skipped", "errors", "skipped_details"} and rep["added"] == 2 and rep["errors"] == []
    # background run: 202 + job + knowledge.indexed event
    r = client.post("/api/knowledge/index", json={"project_id": pid, "sources": ["docs"]})
    assert r.status_code == 202 and r.json()["status"] == "running"
    jid = r.json()["job_id"]
    job = {}
    for _ in range(100):
        job = client.get(f"/api/knowledge/index/{jid}").json()
        if job["status"] != "running":
            break
        import time

        time.sleep(0.05)
    assert job["status"] == "done" and job["report"]["skipped"] == 2 and job["report"]["added"] == 0
    evs = asyncio.run(client.rt.ctx.bus.replay(0, types=[ev.KNOWLEDGE_INDEXED]))
    assert len(evs) == 2 and evs[-1].payload["job_id"] == jid and evs[-1].payload["skipped"] == 2

    s = client.post("/api/knowledge/search", json={"query": "flux capacitor", "project_id": pid}).json()
    assert s["used"] == {"fts": True, "vectors": False}
    top = s["results"][0]
    assert set(top) == {"doc_id", "kind", "title", "source_path", "project_id", "updated_at", "score", "snippet"} and top["source_path"] == "README.md"
    assert client.post("/api/knowledge/search", json={"query": "flux", "kinds": ["bogus"]}).status_code == 422
    assert client.post("/api/knowledge/search", json={"query": "flux", "project_id": "prj_x"}).status_code == 404
    assert client.post("/api/knowledge/search", json={"query": 'NEAR("a" b) OR title:x'}).status_code == 200

    lst = client.get("/api/knowledge/docs", params={"project_id": pid, "kind": "doc", "limit": 1}).json()
    assert lst["total"] == 2 and len(lst["docs"]) == 1 and "body" not in lst["docs"][0] and set(lst) == {"total", "limit", "offset", "docs"}
    page2 = client.get("/api/knowledge/docs", params={"project_id": pid, "limit": 1, "offset": 1}).json()
    assert page2["docs"][0]["id"] != lst["docs"][0]["id"]
    d = client.get(f"/api/knowledge/docs/{top['doc_id']}").json()
    assert "flux capacitor" in d["body"] and d["source_hash"]
    st = client.get("/api/knowledge/stats").json()
    assert st["docs"] == 2 and st["by_kind"] == {"doc": 2} and st["vector_search"]["enabled"] is False
    assert client.delete(f"/api/knowledge/docs/{top['doc_id']}").status_code == 204
    assert client.get(f"/api/knowledge/docs/{top['doc_id']}").status_code == 404
    assert client.delete(f"/api/knowledge/docs/{top['doc_id']}").status_code == 404
    assert client.post("/api/knowledge/search", json={"query": "flux capacitor"}).json()["results"] == []


def test_api_index_validation(client):
    assert client.post("/api/knowledge/index", json={"project_id": "prj_none"}).status_code == 404
    root = client.tmp / "r2"
    root.mkdir()
    pid = client.post("/api/projects", json={"name": "r2", "path": str(root)}).json()["id"]
    r = client.post("/api/knowledge/index", json={"project_id": pid, "globs": ["../*.md"], "wait": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    assert client.post("/api/knowledge/index", json={"sources": ["nope"]}).status_code == 422
    assert client.get("/api/knowledge/index/kjob_none").status_code == 404
