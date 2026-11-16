"""FastAPI dependencies: runtime access and bearer-token auth."""

from __future__ import annotations

from fastapi import Depends, Request

from orchestrator.api.runtime import Runtime
from orchestrator.errors import OrchestratorError
from orchestrator.security.auth import bearer_from_header, token_matches


class Unauthorized(OrchestratorError):
    code = "unauthorized"
    status = 401


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


def require_token(request: Request) -> None:
    rt: Runtime = request.app.state.rt
    supplied = bearer_from_header(request.headers.get("authorization"))
    if not token_matches(rt.token, supplied):
        raise Unauthorized("missing or invalid bearer token")


AUTH = [Depends(require_token)]
