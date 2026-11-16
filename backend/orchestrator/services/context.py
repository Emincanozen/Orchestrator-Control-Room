"""Shared runtime context passed to services (no globals)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.config import CONFIG_DIR, Paths, Policies, effective_policies
from orchestrator.db import records
from orchestrator.db.database import Database, Tx
from orchestrator.events.bus import EventBus


@dataclass
class AppContext:
    db: Database
    bus: EventBus
    paths: Paths
    config_dir: Path = field(default=CONFIG_DIR)

    def policies_tx(self, tx: Tx) -> Policies:
        return effective_policies(records.settings_all(tx), self.config_dir)

    async def policies(self) -> Policies:
        return await self.db.read(self.policies_tx)

    @classmethod
    def open(cls, paths: Paths, config_dir: Path | None = None) -> "AppContext":
        paths.ensure()
        db = Database(paths.db_path).open()
        return cls(db=db, bus=EventBus(db), paths=paths, config_dir=config_dir or CONFIG_DIR)

    def close(self) -> None:
        self.db.close()
