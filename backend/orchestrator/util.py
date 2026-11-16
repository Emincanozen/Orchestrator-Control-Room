"""Small shared helpers: ids, timestamps, JSON."""

from __future__ import annotations

import json
import threading
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """ULID-like id: 48-bit ms timestamp + 80 random bits, Crockford base32, with a type prefix."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"


def utcnow() -> datetime:
    return datetime.now(UTC)


_ts_lock = threading.Lock()
_last_ts: datetime | None = None


def now_iso() -> str:
    """Strictly increasing UTC timestamp within this process.

    The Windows system clock can tick in ~15 ms steps; without this, records
    written in quick succession would tie and their order (e.g. "is this
    verification newer than that run?") would be ambiguous.
    """
    global _last_ts
    with _ts_lock:
        now = utcnow()
        if _last_ts is not None and now <= _last_ts:
            now = _last_ts + timedelta(microseconds=1)
        _last_ts = now
    return now.isoformat(timespec="microseconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def seconds_between(start: str | None, end: str | None) -> float:
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def truncate(text: str, limit: int, *, keep: str = "tail") -> str:
    """Bound text size. keep='tail' keeps the end (most useful for command output)."""
    if len(text) <= limit:
        return text
    marker = f"...[truncated {len(text) - limit} chars]..."
    if keep == "head":
        return text[:limit] + marker
    return marker + text[-limit:]
