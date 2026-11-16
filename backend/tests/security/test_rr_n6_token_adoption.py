"""Re-review N6: an existing API token file must not be adopted without checks.

On load (ACL hardening on): owned by another account, or not restrictable -> a new token (SECURITY log);
otherwise the owner-only permissions are re-applied."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from orchestrator.security import acl, auth

WIN = sys.platform == "win32"
OLD = "o" * 43


@pytest.fixture()
def hardening(monkeypatch):
    monkeypatch.setenv(acl.ENV_HARDEN, "1")


def _plant(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OLD, encoding="ascii")


@pytest.mark.skipif(not WIN, reason="Windows ACL semantics (POSIX: mode bits)")
def test_existing_token_with_loose_acl_is_rehardened_and_kept(tmp_path: Path, hardening, caplog):
    tok = tmp_path / "data" / "api-token"
    _plant(tok)  # inherits the temp dir's ACL: not owner-only
    assert acl.check_private(tok), "control: a freshly written file should not be owner-only"
    with caplog.at_level(logging.WARNING, logger="orchestrator.security.auth"):
        assert auth.load_or_create_token(tok) == OLD
    assert acl.check_private(tok) == []
    assert any("SECURITY: re-applied owner-only" in r.getMessage() for r in caplog.records)


def test_token_owned_by_another_account_is_replaced(tmp_path: Path, hardening, monkeypatch, caplog):
    tok = tmp_path / "data" / "api-token"
    _plant(tok)
    real_check = acl.check_private
    calls = {"n": 0}

    def foreign_owner(path):
        calls["n"] += 1
        if calls["n"] == 1:  # the adopted file: owned by someone else
            return ["owned by S-1-5-21-1-2-3-1001, not by the current user"]
        return real_check(path)

    monkeypatch.setattr(acl, "check_private", foreign_owner)
    with caplog.at_level(logging.WARNING, logger="orchestrator.security.auth"):
        new = auth.load_or_create_token(tok)
    assert new != OLD and len(new) >= 32
    assert tok.read_text(encoding="ascii").strip() == new
    assert any("SECURITY:" in r.getMessage() and "cannot be trusted" in r.getMessage() for r in caplog.records)
    assert not [p for p in tok.parent.iterdir() if p.name.endswith(".tmp")]


def test_token_that_cannot_be_hardened_is_replaced(tmp_path: Path, hardening, monkeypatch):
    tok = tmp_path / "data" / "api-token"
    _plant(tok)
    monkeypatch.setattr(acl, "check_private", lambda p: ["access granted to Everyone (S-1-1-0)"])
    results = iter([False, True])  # hardening the old file fails; hardening the new one works
    monkeypatch.setattr(acl, "harden_private_file", lambda p: next(results))
    new = auth.load_or_create_token(tok)
    assert new != OLD


def test_hardening_off_keeps_the_previous_behaviour(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(acl.ENV_HARDEN, "0")
    tok = tmp_path / "data" / "api-token"
    _plant(tok)
    monkeypatch.setattr(acl, "check_private", lambda p: pytest.fail("must not be called with hardening off"))
    assert auth.load_or_create_token(tok) == OLD


def test_doctor_advice_for_the_token_is_accurate(tmp_path: Path, monkeypatch):
    from orchestrator.cli import doctor

    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(acl.ENV_HARDEN, "1")
    _plant(tmp_path / "data" / "api-token")
    monkeypatch.setattr(acl, "check_private", lambda p: ["inherits permissions from its parent (inheritance not disabled)"])
    text = " ".join(c["fix"] for c in doctor.acl_checks() if c["check"] == "api token permissions")
    assert "replaces the token" in text and "Start the backend once" not in text
