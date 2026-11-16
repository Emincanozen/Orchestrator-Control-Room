"""KnowledgeService: wires indexer + retrieval to the app context. Stored in `rt.extras["knowledge"]`."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional, Sequence

from orchestrator.db import records
from orchestrator.domain import events as ev
from orchestrator.knowledge import retrieval
from orchestrator.knowledge.embedding import Embedder, HashingEmbedder
from orchestrator.knowledge.indexer import Indexer, Report, knowledge_settings, sync_vectors
from orchestrator.services.context import AppContext
from orchestrator.util import new_id

log = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(self, ctx: AppContext, embedder: Optional[Embedder] = None):
        self.ctx = ctx
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.indexer = Indexer(ctx.db)
        self._lock = asyncio.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def settings(self) -> dict[str, Any]:
        return knowledge_settings(await self.ctx.db.read(records.settings_all))

    # ---- indexing ------------------------------------------------------------
    async def index(self, *, project_id: Optional[str] = None, sources: Optional[Iterable[str]] = None, full: bool = False,
                    globs: Optional[list[str]] = None, job_id: Optional[str] = None) -> Report:
        sources = list(sources) if sources else None
        async with self._lock:  # one index run at a time (single writer anyway)
            report = await self.indexer.run(project_id=project_id, sources=sources, full=full, globs=globs)
            cfg = await self.settings()
            await self.ctx.db.write(lambda tx: sync_vectors(tx, self.embedder, cfg["vector_kinds"]))
        d = report.as_dict()
        await self.ctx.bus.emit(ev.KNOWLEDGE_INDEXED, project_id=project_id, payload={
            "job_id": job_id, "project_id": project_id, "sources": sources or [], "added": d["added"], "updated": d["updated"],
            "removed": d["removed"], "skipped": d["skipped"], "error_count": len(report.errors), "errors": d["errors"][:10]})
        return report

    def start_job(self, **kwargs: Any) -> str:
        job_id = new_id("kjob")
        self.jobs[job_id] = {"job_id": job_id, "status": "running", "report": None}
        if len(self.jobs) > 50:
            self.jobs.pop(next(iter(self.jobs)))

        async def run() -> None:
            try:
                rep = await self.index(job_id=job_id, **kwargs)
                self.jobs[job_id].update(status="done", report=rep.as_dict())
            except Exception as exc:  # reported through the job, never raised into the loop
                log.exception("knowledge index failed")
                self.jobs[job_id].update(status="failed", report={"added": 0, "updated": 0, "removed": 0, "skipped": 0,
                                                                  "errors": [f"{exc.__class__.__name__}: {exc}"]})

        t = asyncio.create_task(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return job_id

    async def watch_events(self) -> None:
        """Event-driven indexing: refresh a task's summary / error resolution when it reaches COMPLETED or FAILED."""
        async with self.ctx.bus.subscribe() as sub:
            while True:
                event = await sub.get()
                if event.type != ev.TASK_STATE_CHANGED or event.payload.get("to") not in ("COMPLETED", "FAILED") or not event.task_id:
                    continue
                try:
                    if (await self.settings())["auto_index"]:
                        async with self._lock:
                            await self.indexer.index_task(event.task_id)
                except Exception:
                    log.exception("event-driven knowledge indexing failed for %s", event.task_id)

    # ---- retrieval -------------------------------------------------------------
    async def search(self, query: str, *, project_id: Optional[str] = None, kinds: Optional[Sequence[str]] = None,
                     limit: int = 8) -> dict[str, Any]:
        cfg = await self.settings()

        def fn(tx: Any) -> Any:
            return retrieval.search(tx, query, project_id=project_id, kinds=kinds, limit=limit,
                                    vector_kinds=cfg["vector_kinds"], embedder=self.embedder)

        hits, used = await self.ctx.db.read(fn)
        return {"results": [h.as_dict() for h in hits], "used": used}

    async def context_for_task(self, task: Any, *, budget_chars: int = 4000,
                               error_signature: Optional[str] = None) -> list[retrieval.Snippet]:
        return await self.ctx.db.read(lambda tx: retrieval.context_for_task(
            tx, task, budget_chars=budget_chars, error_signature=error_signature))

    # ---- docs / stats -------------------------------------------------------------
    async def list_docs(self, *, project_id: Optional[str], kind: Optional[str], limit: int, offset: int) -> dict[str, Any]:
        where, params = " WHERE 1=1", []
        if project_id:
            where += " AND project_id = ?"
            params.append(project_id)
        if kind:
            where += " AND kind = ?"
            params.append(kind)
        limit = max(1, min(limit, 500))

        def fn(tx: Any) -> Any:
            total = tx.scalar(f"SELECT COUNT(*) FROM knowledge_docs{where}", params)
            rows = tx.all("SELECT id, project_id, kind, title, source_path, source_hash, updated_at, length(body) AS chars "
                          f"FROM knowledge_docs{where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", [*params, limit, max(0, offset)])
            return total, [dict(r) for r in rows]

        total, rows = await self.ctx.db.read(fn)
        return {"total": total, "limit": limit, "offset": max(0, offset), "docs": rows}

    async def get_doc(self, doc_id: str) -> Optional[dict[str, Any]]:
        r = await self.ctx.db.one(
            "SELECT id, project_id, kind, title, body, source_path, source_hash, updated_at FROM knowledge_docs WHERE id = ?", (doc_id,))
        return dict(r) if r else None

    async def delete_doc(self, doc_id: str) -> bool:
        return bool(await self.ctx.db.write(lambda tx: tx.execute("DELETE FROM knowledge_docs WHERE id = ?", (doc_id,)).rowcount))

    async def stats(self) -> dict[str, Any]:
        cfg = await self.settings()

        def fn(tx: Any) -> Any:
            by_kind = {r["kind"]: r["n"] for r in tx.all("SELECT kind, COUNT(*) AS n FROM knowledge_docs GROUP BY kind")}
            by_proj = {(r["project_id"] or "global"): r["n"]
                       for r in tx.all("SELECT project_id, COUNT(*) AS n FROM knowledge_docs GROUP BY project_id")}
            return {"docs": sum(by_kind.values()), "by_kind": by_kind, "by_project": by_proj,
                    "chars": tx.scalar("SELECT COALESCE(SUM(length(body)),0) FROM knowledge_docs"),
                    "last_updated": tx.scalar("SELECT MAX(updated_at) FROM knowledge_docs"),
                    "vectors": tx.scalar("SELECT COUNT(*) FROM knowledge_vectors")}

        s = await self.ctx.db.read(fn)
        s["vector_search"] = {"enabled": bool(cfg["vector_kinds"]), "kinds": cfg["vector_kinds"], "embedder": self.embedder.name,
                              "semantic": bool(getattr(self.embedder, "semantic", False))}
        return s
