"""Regression checks for token-query failures during the security takeover."""

import sys

import pytest

from orchestrator.security import sandbox


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token queries")
def test_native_elevation_query_uses_exact_sized_buffer():
    import ctypes

    token = sandbox._process_token(sandbox.TOKEN_QUERY)
    try:
        info = sandbox._token_info(token, sandbox.TokenElevation)
        assert len(info) == ctypes.sizeof(ctypes.c_uint32)
        expected = bool(ctypes.c_uint32.from_buffer(info).value)
    finally:
        sandbox._k32.CloseHandle(token)
    assert sandbox.is_elevated() is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token queries")
@pytest.mark.parametrize("stage", ["open", "query"])
def test_unknown_elevation_refuses_start(monkeypatch, stage):
    def fail(*args):
        raise OSError("injected token query failure")

    if stage == "open":
        monkeypatch.setattr(sandbox, "_process_token", fail)
    else:
        monkeypatch.setattr(sandbox, "_token_info", fail)
    with pytest.raises(sandbox.SandboxUnavailable, match="cannot determine"):
        sandbox.refuse_elevated()


def test_serve_reports_unknown_elevation(monkeypatch):
    from typer.testing import CliRunner

    from orchestrator.cli.main import app

    def fail():
        raise sandbox.SandboxUnavailable("cannot determine elevation")

    monkeypatch.setattr(sandbox, "is_elevated", fail)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "cannot determine elevation" in result.output


def test_doctor_reports_unknown_elevation(monkeypatch):
    from orchestrator.cli import doctor

    def fail():
        raise sandbox.SandboxUnavailable("cannot determine elevation")

    monkeypatch.setattr(sandbox, "is_elevated", fail)
    checks = doctor.sandbox_checks()
    assert checks[0]["check"] == "not elevated"
    assert checks[0]["status"] == doctor.FAIL
