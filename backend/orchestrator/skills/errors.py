"""Skills & MCP Manager errors (mapped onto the API error envelope by the app's OrchestratorError handler)."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.errors import OrchestratorError


class SkillError(OrchestratorError):
    code = "skill_error"
    status = 409


class SkillNotFound(SkillError):
    code = "not_found"
    status = 404


class InvalidSkillState(SkillError):
    code = "invalid_state"
    status = 409


class NotInstallable(SkillError):
    code = "not_installable"
    status = 409


class CatalogueOnly(SkillError):
    code = "catalogue_only"
    status = 409


class SourceNotAllowed(SkillError):
    code = "source_not_allowed"
    status = 422


class FetchRejected(SkillError):
    """The fetched content violated a safety rule (traversal, symlink, size, ...)."""

    code = "fetch_rejected"
    status = 422


class FetchFailed(SkillError):
    code = "fetch_failed"
    status = 502


class ActivationRefused(SkillError):
    code = "activation_refused"
    status = 409


class CatalogError(Exception):
    def __init__(self, message: str, problems: Optional[list[Any]] = None):
        super().__init__(message)
        self.problems = problems or []
