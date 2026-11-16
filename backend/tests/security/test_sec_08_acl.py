"""Finding #8 (MEDIUM): the data dir, the token file and the worktrees dir are owner-only.

Real ACLs on real temp directories (icacls / Win32 API on Windows, chmod elsewhere). The suite-wide conftest
disables hardening for speed; these tests enable it explicitly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import get_paths
from orchestrator.security import acl
from orchestrator.security.auth import load_or_create_token

WIN = os.name == "nt"


@pytest.fixture
def harden_on(monkeypatch):
    monkeypatch.setenv(acl.ENV_HARDEN, "1")


def _plain_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plain"
    d.mkdir()
    return d


def test_hardened_dir_reports_clean(tmp_path):
    d = _plain_dir(tmp_path)
    assert acl.harden_private_dir(d) is True
    assert acl.check_private(d) == []


def test_hardened_file_reports_clean(tmp_path):
    f = _plain_dir(tmp_path) / "secret"
    f.write_text("x", encoding="ascii")
    assert acl.harden_private_file(f) is True
    assert acl.check_private(f) == []


@pytest.mark.skipif(not WIN, reason="Windows ACL semantics")
def test_broad_ace_is_reported_and_removed(tmp_path):
    d = _plain_dir(tmp_path)
    r = subprocess.run([acl.icacls_path(), str(d), "/grant", "*S-1-5-32-545:(OI)(CI)R", "*S-1-5-11:(OI)(CI)M"],
                       capture_output=True)
    assert r.returncode == 0
    problems = acl.check_private(d)
    assert any("Users (S-1-5-32-545)" in p for p in problems)
    assert any("Authenticated Users (S-1-5-11)" in p for p in problems)
    assert acl.harden_private_dir(d) is True
    assert acl.check_private(d) == []
    sids = {a["sid"] for a in acl.read_dacl(d)[1]}
    assert sids == {acl.current_user_sid(), acl.SID_SYSTEM, acl.SID_ADMINISTRATORS}


@pytest.mark.skipif(not WIN, reason="Windows ACL semantics")
def test_inherited_acl_is_reported(tmp_path):
    d = _plain_dir(tmp_path)  # plain mkdir: inherits from its parent
    problems = acl.check_private(d)
    assert "inherits permissions from its parent (inheritance not disabled)" in problems
    broad = [p for p in problems if any(name in p for name in acl.BROAD_SIDS.values())]
    if not broad:
        pytest.skip("the temp volume grants no broad principal here; only the inheritance problem is reported")


@pytest.mark.skipif(not WIN, reason="Windows ACL semantics")
def test_checkout_drive_broad_aces_are_detected_when_present():
    """The review found `Authenticated Users: Modify` on the checkout drive. check_private must see such ACEs."""
    from orchestrator.config import REPO_ROOT

    problems = acl.check_private(REPO_ROOT)
    if not any("S-1-5-11" in p or "S-1-5-32-545" in p for p in problems):
        pytest.skip("the checkout has no broad ACEs on this machine")
    assert problems


def test_current_user_sid_shape():
    if not WIN:
        pytest.skip("Windows only")
    sid = acl.current_user_sid()
    assert sid.startswith("S-1-5-") and sid.count("-") >= 3


def test_ensure_hardens_data_and_worktrees_dirs(tmp_path, harden_on):
    p = get_paths(tmp_path / "data").ensure()
    assert acl.check_private(p.data_dir) == []
    assert acl.check_private(p.worktrees_dir) == []


def test_ensure_respects_the_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv(acl.ENV_HARDEN, "0")
    p = get_paths(tmp_path / "data").ensure()
    if WIN:
        assert acl.check_private(p.data_dir)  # untouched: still inherits


def test_new_token_file_is_owner_only(tmp_path, harden_on):
    d = _plain_dir(tmp_path)
    load_or_create_token(d / "api-token")
    assert acl.check_private(d / "api-token") == []


def test_hardening_failure_is_logged_not_raised(tmp_path, caplog, monkeypatch):
    missing = tmp_path / "does-not-exist"
    assert acl.harden_private_dir(missing) is False
    assert any("SECURITY" in r.getMessage() for r in caplog.records)
    assert acl.check_private(missing)  # and the check reports it


@pytest.mark.skipif(not WIN, reason="Windows only")
def test_icacls_is_taken_from_system32(monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    assert acl.icacls_path().lower() == "c:\\windows\\system32\\icacls.exe"


def test_doctor_reports_acl_state(tmp_path, monkeypatch):
    from orchestrator.cli import doctor

    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ORCHESTRATOR_WORKTREES_DIR", raising=False)
    p = get_paths().ensure(harden=False)
    checks = {c["check"]: c for c in doctor.acl_checks()}
    if WIN:
        assert checks["data dir permissions"]["status"] == doctor.WARN
    acl.harden_private_dir(p.data_dir)
    acl.harden_private_dir(p.worktrees_dir)
    checks = {c["check"]: c for c in doctor.acl_checks()}
    assert checks["data dir permissions"]["status"] == doctor.OK
    assert checks["worktrees dir permissions"]["status"] == doctor.OK


def test_doctor_flags_a_nested_configuration(tmp_path, monkeypatch):
    from orchestrator.cli import doctor

    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ORCHESTRATOR_WORKTREES_DIR", str(tmp_path / "d" / "w"))
    [c] = doctor.acl_checks()
    assert c["status"] == doctor.FAIL
    assert doctor.data_dir_check()["status"] == doctor.FAIL


def test_doctor_warns_about_a_legacy_data_dir(tmp_path):
    from orchestrator.cli import doctor

    legacy = tmp_path / "data"
    assert doctor.legacy_data_check(legacy) is None
    legacy.mkdir()
    (legacy / "orchestrator.db").write_bytes(b"")
    c = doctor.legacy_data_check(legacy)
    assert c is not None and c["status"] == doctor.WARN and "no longer used" in c["detail"]
