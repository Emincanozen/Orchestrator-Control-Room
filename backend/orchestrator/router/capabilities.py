"""Capability vocabulary, tier ordering and the model capability registry (ARCHITECTURE section 9).

Pure: built from discovered ``ModelInfo`` lists plus the parsed ``config/models.yaml``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..providers.base import ModelInfo

CORE_CAPABILITIES: tuple[str, ...] = ("coding", "planning", "review", "research", "documentation")
VISUAL_CAPABILITIES: tuple[str, ...] = (
    "image_understanding",
    "image_generation",
    "image_editing",
    "ui_implementation",
    "visual_design",
    "browser_inspection",
    "screenshot_analysis",
    "pixel_art",
    "animation",
    "video_generation",
)
ALL_CAPABILITIES: frozenset[str] = frozenset(CORE_CAPABILITIES + VISUAL_CAPABILITIES)

TIER_ORDER: tuple[str, ...] = ("fast", "standard", "advanced", "frontier")
Tier = Literal["fast", "standard", "advanced", "frontier"]
DEFAULT_TIER: Tier = "standard"

# Capability X is considered present when any of the listed capabilities is present
# (plain writing ability is covered by any text-capable model; there is no separate flag in the CLIs).
IMPLIED_BY: dict[str, tuple[str, ...]] = {"documentation": ("research", "coding", "planning")}

Verification = Literal["verified", "listed", "unlisted"]
TierSource = Literal["config", "default"]


def tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError as exc:
        raise ValueError(f"unknown tier {tier!r}") from exc


def max_tier(*tiers: str) -> str:
    return max(tiers, key=tier_rank)


class ModelEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    tier: Tier
    tier_source: TierSource
    capabilities: frozenset[str]  # verified provider-level capabilities
    listed_capabilities: frozenset[str] = frozenset()  # advertised, never exercised
    billing_class: str
    verification: Verification
    alias: str | None = None
    source: str = "config"


class CapabilityRegistry:
    def __init__(self, models_cfg: Mapping[str, Any] | None = None, discovered: Iterable[ModelInfo] = ()) -> None:
        self._entries: dict[tuple[str, str], ModelEntry] = {}
        self._aliases: dict[tuple[str, str], str] = {}
        providers = (models_cfg or {}).get("providers") or {}
        provider_caps: dict[str, frozenset[str]] = {}
        provider_listed: dict[str, frozenset[str]] = {}
        provider_billing: dict[str, str] = {}

        for pname, pcfg in providers.items():
            pcfg = pcfg or {}
            caps = frozenset(pcfg.get("task_capabilities") or [])
            listed = frozenset(pcfg.get("listed_unverified_capabilities") or []) - caps
            billing = str(pcfg.get("billing_class") or "unknown")
            provider_caps[pname], provider_listed[pname], provider_billing[pname] = caps, listed, billing
            for m in pcfg.get("models") or []:
                mid = str(m["id"])
                level: Verification = "verified" if m.get("verified") is True else "listed"
                self._entries[(pname, mid)] = ModelEntry(
                    provider=pname, model_id=mid, tier=m.get("tier", DEFAULT_TIER), tier_source="config",
                    capabilities=caps, listed_capabilities=listed, billing_class=billing,
                    verification=level, alias=m.get("alias"), source="config",
                )
                if m.get("alias"):
                    self._aliases[(pname, str(m["alias"]))] = mid

        for info in discovered:
            key = (info.provider, self._aliases.get((info.provider, info.model_id), info.model_id))
            if key in self._entries:
                continue
            caps = provider_caps.get(info.provider)
            if caps is None:
                caps = frozenset(info.capabilities)
            self._entries[key] = ModelEntry(
                provider=info.provider, model_id=key[1], tier=DEFAULT_TIER, tier_source="default",
                capabilities=caps, listed_capabilities=provider_listed.get(info.provider, frozenset()),
                billing_class=provider_billing.get(info.provider, "unknown"),
                verification="unlisted", source=info.source,
            )

    def entries(self) -> list[ModelEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def providers(self) -> list[str]:
        return sorted({k[0] for k in self._entries})

    def get(self, provider: str, model: str) -> ModelEntry | None:
        return self._entries.get((provider, self._aliases.get((provider, model), model)))

    def effective_capabilities(self, entry: ModelEntry, allow_listed: bool = False) -> frozenset[str]:
        caps = set(entry.capabilities)
        if allow_listed:
            caps |= entry.listed_capabilities
        for cap, sources in IMPLIED_BY.items():
            if any(s in caps for s in sources):
                caps.add(cap)
        return frozenset(caps)

    def missing_capabilities(
        self, entry: ModelEntry, required: Iterable[str], allow_listed: bool = False
    ) -> tuple[list[str], list[str]]:
        """Return (missing, listed_only): missing = unusable; listed_only = would be usable with allow_listed."""
        have = self.effective_capabilities(entry, allow_listed)
        missing = sorted(c for c in set(required) if c not in have)
        listed_only = [c for c in missing if c in entry.listed_capabilities]
        return missing, listed_only
