"""Local-first knowledge (RAG): FTS5 over `knowledge_docs` with provenance, optional lexical-hash vectors.

MEMORY SEPARATION (binding): knowledge is *reference material*, never task state. Task state lives in
`tasks` / `runs` / `checkpoints` / `verifications` and is owned by the scheduler and services. This package
only ever WRITES to `knowledge_docs` (and its FTS index) and `knowledge_vectors`; it reads tasks/runs/etc.
for summaries and never writes them (enforced by a source-scan test). Retrieved text is labelled untrusted
reference data by `context_for_task`; it must not be used to decide task state, permissions or routing.

Wire-up (done by the architect in api/app.py):

    from orchestrator.api.routes import knowledge as knowledge_routes
    from orchestrator import knowledge
    create_app(extra_routers=[knowledge_routes.router], on_startup=[knowledge.startup])

`startup(rt)` stores `KnowledgeService` in `rt.extras["knowledge"]` and starts the event-driven indexer for
COMPLETED / FAILED tasks (it honours setting `knowledge.auto_index`, default true). Nothing is embedded
automatically: vectors exist only for kinds listed in setting `knowledge.vectors.kinds` and never replace FTS.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from orchestrator.knowledge.embedding import Embedder, HashingEmbedder
from orchestrator.knowledge.retrieval import Snippet, context_for_task, render_context
from orchestrator.knowledge.service import KnowledgeService

__all__ = ["KnowledgeService", "Embedder", "HashingEmbedder", "Snippet", "context_for_task", "render_context", "startup",
           "make_startup"]


def make_startup(*, embedder: Optional[Embedder] = None, watch: bool = True) -> Callable[[Any], Awaitable[None]]:
    async def _startup(rt: Any) -> None:
        svc = KnowledgeService(rt.ctx, embedder=embedder)
        rt.extras["knowledge"] = svc
        if watch:
            rt.extras["knowledge_watcher"] = asyncio.create_task(svc.watch_events())

    return _startup


startup = make_startup()
