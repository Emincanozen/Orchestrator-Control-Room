from __future__ import annotations

import pytest
from router_helpers import load

from orchestrator.router import CapabilityRegistry, Router


@pytest.fixture(scope="session")
def models_cfg() -> dict:
    return load("models.yaml")


@pytest.fixture(scope="session")
def routing_cfg() -> dict:
    return load("routing.yaml")


@pytest.fixture(scope="session")
def policies_cfg() -> dict:
    return load("policies.yaml")


@pytest.fixture
def registry(models_cfg) -> CapabilityRegistry:
    return CapabilityRegistry(models_cfg)


@pytest.fixture
def router(routing_cfg, policies_cfg, registry) -> Router:
    return Router(routing_cfg, policies_cfg, registry)
