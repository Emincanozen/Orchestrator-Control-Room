"""Domain errors that map onto the API error envelope (ARCHITECTURE §6)."""

from __future__ import annotations

from typing import Any, Optional


class OrchestratorError(Exception):
    code = "error"
    status = 400

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class NotFound(OrchestratorError):
    code = "not_found"
    status = 404


class Conflict(OrchestratorError):
    code = "conflict"
    status = 409


class ValidationFailed(OrchestratorError):
    code = "validation_failed"
    status = 422


class DependencyCycle(ValidationFailed):
    code = "dependency_cycle"
