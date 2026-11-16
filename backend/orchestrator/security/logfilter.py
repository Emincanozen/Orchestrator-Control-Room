"""Logging filter that keeps API tokens out of server logs.

uvicorn logs the request line of every HTTP request and WebSocket handshake, including the query string, so a
`/ws?token=...` URL would end up in server.log. `TokenRedactingFilter` rewrites `token=<value>` query values
(and anything `security.redact` knows) in the record's message and arguments. It never drops a record and never
raises. `install_on_uvicorn_loggers()` attaches it to the uvicorn loggers; it is idempotent.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from orchestrator.security.redact import MASK, redact

# `?token=abc`, `&token=abc`, also URL-encoded separators and the `access_token` spelling.
_TOKEN_QUERY = re.compile(r"((?:[?&]|%3[fF]|%26)(?:access_)?token=)[^&\s\"'#]+", re.IGNORECASE)


def scrub(text: str) -> str:
    return redact(_TOKEN_QUERY.sub(r"\1" + MASK, text))


def _scrub_arg(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, tuple):
        return tuple(_scrub_arg(v) for v in value)
    if isinstance(value, list):
        return [_scrub_arg(v) for v in value]
    return value


class TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_scrub_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
        except Exception:  # noqa: BLE001 - a logging filter must never break logging
            pass
        return True


UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")


def install_on_uvicorn_loggers() -> None:
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in lg.filters):
            lg.addFilter(TokenRedactingFilter())


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default LOGGING_CONFIG with the redacting filter attached to its handlers (uvicorn rebuilds
    logger filters when it applies the config, so the handlers are the place that survives)."""
    import copy

    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_token"] = {"()": "orchestrator.security.logfilter.TokenRedactingFilter"}
    for handler in cfg.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_token")
    return cfg
