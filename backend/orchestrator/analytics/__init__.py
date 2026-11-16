"""Analytics (ARCHITECTURE §11): metrics computed from real rows with explicit exactness.

Wire-up (done by the architect in api/app.py):

    from orchestrator.api.routes import analytics as analytics_routes
    from orchestrator import analytics
    create_app(extra_routers=[analytics_routes.router], on_startup=[analytics.startup])

`startup(rt)` stores `AnalyticsService` in `rt.extras["analytics"]`.
"""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.analytics import adaptive, metrics
from orchestrator.config import load_yaml
from orchestrator.db import records
from orchestrator.services.context import AppContext

__all__ = ["AnalyticsService", "startup", "metrics", "adaptive"]


class AnalyticsService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def routing_cfg(self) -> dict[str, Any]:
        return load_yaml("routing.yaml", self.ctx.config_dir)

    async def summary(self, scope: metrics.Scope) -> dict[str, Any]:
        return await self.ctx.db.read(lambda tx: metrics.summary(tx, scope))

    async def timeseries(self, scope: metrics.Scope, name: str) -> dict[str, Any]:
        return await self.ctx.db.read(lambda tx: metrics.timeseries(tx, scope, name))

    async def agents(self, scope: metrics.Scope) -> list[dict[str, Any]]:
        return await self.ctx.db.read(lambda tx: metrics.agents(tx, scope))

    async def models(self, **filters: Optional[str]) -> list[dict[str, Any]]:
        return await self.ctx.db.read(lambda tx: metrics.model_table(tx, **filters))

    async def routing_report(self) -> dict[str, Any]:
        cfg = self.routing_cfg()
        return await self.ctx.db.read(lambda tx: adaptive.report(tx, records.settings_all(tx), cfg))

    async def get_adaptive_settings(self) -> dict[str, Any]:
        cfg = self.routing_cfg()
        return await self.ctx.db.read(lambda tx: adaptive.settings_view(records.settings_all(tx), cfg))

    async def put_adaptive_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        await self.ctx.db.write(lambda tx: adaptive.save_settings(tx, patch))
        return await self.get_adaptive_settings()


async def startup(rt: Any) -> None:
    rt.extras["analytics"] = AnalyticsService(rt.ctx)
