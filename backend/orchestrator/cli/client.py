"""HTTP client core of the `orchestrator` CLI.

The CLI owns no state: every command is a request to the running backend (same API and database as the
dashboard). Base URL: `--url` > env ORCHESTRATOR_URL > policies (security.bind_host / security.port).
Token: read from `<data dir>/api-token` (env ORCHESTRATOR_TOKEN_FILE overrides the path). The token is never
accepted on the command line and never printed.

Exit codes carried by the exceptions: 1 = API / usage error, 2 = backend unreachable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from orchestrator.config import effective_policies, get_paths

ENV_URL = "ORCHESTRATOR_URL"
ENV_TOKEN_FILE = "ORCHESTRATOR_TOKEN_FILE"
START_HINT = "start it with `orchestrator serve` or `orchestrator dashboard`"

# Tokens seen in this process; scrubbed from every message the CLI prints.
_known_tokens: set[str] = set()


def scrub(text: str) -> str:
    for t in _known_tokens:
        if t and t in text:
            text = text.replace(t, "***")
    return text


class CliError(Exception):
    exit_code = 1


class Unreachable(CliError):
    exit_code = 2


class TokenMissing(CliError):
    pass


class ApiError(CliError):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def is_missing_endpoint(self) -> bool:
        """True when the route itself does not exist (or its service is not started) in this build."""
        if self.status == 404 and self.message.strip().lower() in ("not found", "404: not found"):
            return True
        return "is not started" in self.message


class Unauthorized(ApiError):
    pass


def default_url() -> str:
    security = effective_policies({}).security
    return f"http://{security.get('bind_host', '127.0.0.1')}:{int(security.get('port', 8765))}"


def resolve_url(url: Optional[str] = None) -> str:
    return (url or os.environ.get(ENV_URL) or default_url()).rstrip("/")


def token_path() -> Path:
    override = os.environ.get(ENV_TOKEN_FILE)
    return Path(override) if override else get_paths().token_path


def read_token() -> str:
    path = token_path()
    try:
        token = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise TokenMissing(
            f"API token file not found: {path}. The backend creates it on first start ({START_HINT}); "
            f"if your data lives elsewhere set ORCHESTRATOR_DATA_DIR or ORCHESTRATOR_TOKEN_FILE."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenMissing(f"cannot read the API token file {path}: {exc.__class__.__name__}") from None
    if not token:
        raise TokenMissing(f"API token file {path} is empty; delete it and restart the backend to regenerate it.")
    _known_tokens.add(token)
    return token


# Tests (and embedders) can inject any httpx.Client (Starlette TestClient, ASGI transport, ...).
ClientFactory = Callable[[str], httpx.Client]
_client_factory: Optional[ClientFactory] = None


def set_client_factory(factory: Optional[ClientFactory]) -> None:
    global _client_factory
    _client_factory = factory


def client_injected() -> bool:
    return _client_factory is not None


@dataclass
class Result:
    status: int
    data: Any


class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.Client] = None
        self._token: Optional[str] = None

    # ---- plumbing ------------------------------------------------------------------------------
    def _http(self) -> httpx.Client:
        if self._client is None:
            if _client_factory is not None:
                self._client = _client_factory(self.base_url)
            else:
                # trust_env=False: never route a loopback call through a proxy from the environment.
                self._client = httpx.Client(
                    base_url=self.base_url, timeout=httpx.Timeout(60.0, connect=3.0), trust_env=False
                )
        return self._client

    def token(self) -> str:
        if self._token is None:
            self._token = read_token()
        return self._token

    def close(self) -> None:
        if self._client is not None and _client_factory is None:
            self._client.close()

    def _unreachable(self) -> Unreachable:
        return Unreachable(f"cannot reach the backend at {self.base_url}: {START_HINT}.")

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        auth: bool = True,
        timeout: Optional[float] = None,
    ) -> Result:
        headers: dict[str, str] = {}
        if auth:
            try:
                headers["Authorization"] = f"Bearer {self.token()}"
            except TokenMissing:
                # No token and no server is "unreachable" first: that is the actionable message.
                self.health()
                raise
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        kw: dict[str, Any] = {"params": clean or None, "headers": headers}
        if json is not None:
            kw["json"] = json
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            resp = self._http().request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise self._unreachable() from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            raise ApiError(0, "timeout", f"the backend did not answer {method} {path} in time") from None
        except httpx.TransportError:
            raise Unreachable(f"the connection to the backend at {self.base_url} was lost: {START_HINT}.") from None
        return self._result(resp)

    def _result(self, resp: httpx.Response) -> Result:
        if resp.status_code < 400:
            if resp.status_code == 204 or not resp.content:
                return Result(resp.status_code, None)
            try:
                return Result(resp.status_code, resp.json())
            except ValueError:
                return Result(resp.status_code, resp.text)
        code, message, details = "error", resp.reason_phrase or f"HTTP {resp.status_code}", {}
        try:
            env = resp.json().get("error")
            if isinstance(env, dict):
                code = str(env.get("code") or code)
                message = str(env.get("message") or message)
                details = env.get("details") if isinstance(env.get("details"), dict) else {}
        except (ValueError, AttributeError):
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            if snippet:
                message = f"{message}: {snippet}"
        if resp.status_code == 401:
            raise Unauthorized(
                401, code,
                f"the backend rejected the API token (401). Token file used: {token_path()}. It must belong to the "
                f"same data dir as the running backend (ORCHESTRATOR_DATA_DIR / ORCHESTRATOR_TOKEN_FILE); "
                f"if the file was recreated, restart the backend.",
                details,
            )
        raise ApiError(resp.status_code, code, message, details)

    # ---- verbs ---------------------------------------------------------------------------------
    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params).data

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.call("POST", path, json=body, **kw).data

    def put(self, path: str, body: Any = None) -> Any:
        return self.call("PUT", path, json=body).data

    def patch(self, path: str, body: Any = None) -> Any:
        return self.call("PATCH", path, json=body).data

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path).data

    def health(self) -> dict[str, Any]:
        """GET /api/health (no auth). Raises Unreachable when nothing answers."""
        data = self.call("GET", "/api/health", auth=False).data
        return data if isinstance(data, dict) else {}


def is_running(base_url: str) -> bool:
    """True when something answers /api/health with status ok at this URL."""
    api = Api(base_url)
    try:
        return api.health().get("status") == "ok"
    except CliError:
        return False
    finally:
        api.close()
