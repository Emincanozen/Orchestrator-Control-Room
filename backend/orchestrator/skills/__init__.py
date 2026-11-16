"""Skills & MCP Manager (ARCHITECTURE §11).

Wire-up (done by the architect in api/app.py):

    from orchestrator.api.routes import skills as skills_routes
    from orchestrator import skills
    create_app(extra_routers=[skills_routes.router], on_startup=[skills.startup])

`startup(rt)` builds the SkillManager (stored in `rt.extras["skills"]`) and imports config/skills-catalog.yaml.
`make_startup(**manager_kwargs)` returns the same hook with an injected source policy / `which` / platform (tests).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from orchestrator.skills.errors import CatalogError
from orchestrator.skills.manager import SkillManager

log = logging.getLogger(__name__)

__all__ = ["SkillManager", "startup", "make_startup"]


def make_startup(catalog_path: Any = None, **manager_kwargs: Any) -> Callable[[Any], Awaitable[None]]:
    async def _startup(rt: Any) -> None:
        manager = SkillManager(rt.ctx, rt.approvals, **manager_kwargs)
        rt.extras["skills"] = manager
        try:
            report = await manager.import_catalog(catalog_path)
            rt.extras["skills_import"] = report.as_dict()
            log.info("skills catalog imported: %d created, %d updated, %d unchanged", report.created, report.updated, report.unchanged)
        except (CatalogError, OSError) as exc:
            # a broken catalog must not stop the app; the endpoints keep serving what is already in the DB
            log.error("skills catalog import failed: %s %s", exc, getattr(exc, "problems", ""))
            rt.extras["skills_import"] = {"error": str(exc)}

    return _startup


startup = make_startup()
