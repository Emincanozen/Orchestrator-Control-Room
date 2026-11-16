"""Secret redaction for any text that is logged, stored as output or emitted.

Pattern-based plus an explicit registry of known secret values (e.g. the API
token). Applied by the event bus to every payload, so callers cannot forget.
"""

from __future__ import annotations

import re
import threading
from typing import Any

MASK = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), MASK),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|basic|token)?\s*[A-Za-z0-9._~+/=\-]{8,}"), r"\1" + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer " + MASK),
    # Provider keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), MASK),
    (re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), MASK),
    (re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), MASK),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), MASK),
    # URL credentials  scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),
    # key=value / "key": "value" for secret-ish names
    (
        re.compile(
            r"(?i)(\b[\w\-]*(?:api[_\-]?key|secret|password|passwd|access[_\-]?token|auth[_\-]?token|"
            r"refresh[_\-]?token|client[_\-]?secret|private[_\-]?key)[\w\-]*[\"']?\s*[:=]\s*[\"']?)"
            r"([^\s\"',;}{]{4,})"
        ),
        r"\1" + MASK,
    ),
]

_known_lock = threading.Lock()
_known_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Always mask this exact value (min length 8 to avoid masking common words)."""
    if value and len(value) >= 8:
        with _known_lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _known_lock:
        known = sorted(_known_secrets, key=len, reverse=True)
    for secret in known:
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings in JSON-like data (bounded depth)."""
    if _depth > 32:
        return MASK
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, _depth + 1) for v in value]
    return value
