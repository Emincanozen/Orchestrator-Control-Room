"""Bounded subscription snapshots; no source modification or secret-bearing errors."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from orchestrator.security import paths

MAX_AUTH_BYTES = 1024 * 1024


class CodexAuthUnavailable(RuntimeError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def validate_auth_snapshot(auth_bytes: bytes) -> str:
    """Shared strict contract; return identity only, clear secret locals before refusal."""
    valid, identity = False, ""
    obj = tokens = account = None
    try:
        if type(auth_bytes) is bytes and 0 < len(auth_bytes) <= MAX_AUTH_BYTES:
            obj = json.loads(auth_bytes.decode("utf-8"), object_pairs_hook=_no_duplicates,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if type(obj) is dict and set(obj) <= {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}:
                tokens = obj.get("tokens")
                valid = (obj.get("auth_mode") == "chatgpt" and obj.get("OPENAI_API_KEY") is None
                         and type(tokens) is dict
                         and set(tokens) <= {"access_token", "refresh_token", "id_token", "account_id"})
                if valid:
                    valid = all(type(tokens.get(k)) is str and bool(tokens[k].strip())
                                for k in ("access_token", "refresh_token", "id_token"))
                    account = tokens.get("account_id")
                    valid = valid and (account is None or (type(account) is str and 0 < len(account) <= 1024
                                                          and all(ord(c) >= 32 for c in account)))
                    valid = valid and (obj.get("last_refresh") is None or type(obj["last_refresh"]) is str)
                    if valid:
                        identity = "account:" + account if account else "snapshot:" + hashlib.sha256(auth_bytes).hexdigest()
    except Exception:  # noqa: BLE001 - discard parser context; raise sanitized failure outside handler
        valid = False
    finally:
        auth_bytes = b""
        obj = tokens = account = None
    # Outside the handler: no JSONDecodeError.doc or parser traceback in __context__.
    if not valid:
        identity = ""
        raise CodexAuthUnavailable("Unsupported subscription authentication snapshot")
    return identity


def read_subscription_auth(environment: Mapping[str, str]) -> bytes:
    """Read exactly one no-follow snapshot. Never copy settings or retain failed raw data."""
    raw = result = b""
    normalized = {}
    home = home_value = profile = source = key = value = name = None
    error = "Subscription credentials are unavailable or unsafe"
    try:
        for key, value in environment.items():
            name = key.upper()
            if name in normalized and normalized[name] != value:
                error = "Ambiguous credential environment"
                raise ValueError()
            normalized[name] = value
        if normalized.get("OPENAI_API_KEY"):
            error = "API-key authentication is not authorized for this runtime"
            raise ValueError()
        home_value = normalized.get("CODEX_HOME")
        if home_value:
            home = Path(home_value)
        else:
            profile = normalized.get("USERPROFILE") or normalized.get("HOME")
            home = (Path(profile) if profile else Path.home()) / ".codex"
        if not home.is_absolute():
            error = "Subscription credential home must be absolute"
            raise ValueError()
        with paths.open_inside(home, "auth.json") as source:
            raw = source.read(MAX_AUTH_BYTES + 1)
        validate_auth_snapshot(raw)
        result = raw
    except Exception:  # noqa: BLE001 - source/parser failures must never retain credential diagnostics
        result = b""
    finally:
        raw = b""
        environment = None
        normalized.clear()
        home = home_value = profile = source = key = value = name = None
    if not result:
        raise CodexAuthUnavailable(error)
    return result
