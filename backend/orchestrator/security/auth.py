"""API token: random, stored in `<data dir>/api-token`, created on first start.

The file is made owner-only (security/acl.py). That keeps OTHER local accounts out; it does not keep out
processes of the same user. Agents are kept away from it by the provider confinement and by never running a
provider with a working directory in or around the data dir (providers/process.py::check_run_paths)."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

from orchestrator.security import acl
from orchestrator.security.redact import register_secret

log = logging.getLogger(__name__)


def _write_new_token(path: Path, *, replace: bool) -> None:
    """Write a fresh token. `replace`: atomically swap it in for an existing (untrusted) file."""
    token = secrets.token_urlsafe(32)
    if not replace:
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return  # created concurrently; the caller reads it
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token)
        return
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(token)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"SECURITY: cannot replace the untrusted API token file {path} ({exc}); delete it "
                           "and start again") from exc


def _owned_by_someone_else(problems: list[str]) -> bool:
    return any(p.startswith("owned by ") for p in problems)


def load_or_create_token(path: Path) -> str:
    """Return the API token, creating it on first start.

    An existing file is not adopted blindly (re-review N6): with ACL hardening on, a file owned by another
    account, or one whose permissions cannot be made owner-only, is replaced by a new token (logged as
    SECURITY); otherwise its permissions are re-hardened. Anyone holding the old token loses access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    harden = acl.hardening_enabled()
    if not path.exists():
        _write_new_token(path, replace=False)
        if harden:
            acl.harden_private_file(path)  # logs loudly on failure; never raises
    elif harden:
        problems = acl.check_private(path)
        if problems:
            reason = "; ".join(problems)
            if _owned_by_someone_else(problems) or not acl.harden_private_file(path) or acl.check_private(path):
                log.warning("SECURITY: the API token file %s cannot be trusted (%s); generating a new token. "
                            "Reopen the dashboard with `orchestrator dashboard`.", path, reason)
                _write_new_token(path, replace=True)
                if not acl.harden_private_file(path):
                    log.error("SECURITY: the new API token file %s could not be made owner-only", path)
            else:
                log.warning("SECURITY: re-applied owner-only permissions to the API token file %s (%s)", path, reason)
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise RuntimeError(f"API token in {path} is too short; delete it to regenerate")
    register_secret(token)
    return token


def token_matches(expected: str, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def bearer_from_header(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
