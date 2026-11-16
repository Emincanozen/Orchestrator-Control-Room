"""Native AppContainer checks; failures on Windows are never converted to skips."""

import os
from pathlib import Path

import pytest

from orchestrator.security import appcontainer, sandbox


def test_identity_is_task_and_installation_scoped(tmp_path):
    assert appcontainer.profile_name(tmp_path / "a") == appcontainer.profile_name(tmp_path / "a")
    assert appcontainer.profile_name(tmp_path / "a") != appcontainer.profile_name(tmp_path / "b")
    assert len(appcontainer.profile_name(tmp_path)) <= 64


def test_loopback_exemption_is_refused_without_changing_it(monkeypatch):
    sid = "S-1-15-2-123"
    monkeypatch.setattr(appcontainer, "loopback_exemptions", lambda: {sid})
    with pytest.raises(appcontainer.AppContainerUnavailable, match="loopback exemption"):
        appcontainer.refuse_loopback_exemption(sid)
    appcontainer.refuse_loopback_exemption("S-1-15-2-456")


@pytest.mark.skipif(os.name != "nt", reason="Windows API")
def test_native_profile_identity_and_cleanup(tmp_path):
    profile = appcontainer.Profile(tmp_path / "task")
    try:
        profile.create()
        caps = profile.capabilities()
        assert caps.CapabilityCount == 0
        assert not caps.Capabilities
        assert appcontainer.task_sid(tmp_path / "task") == profile.sid
        duplicate = appcontainer.Profile(tmp_path / "task")
        with pytest.raises(appcontainer.AppContainerUnavailable, match="CreateAppContainerProfile"):
            duplicate.create()
        duplicate.close()  # must never delete another instance's profile
        assert profile.capabilities().AppContainerSid
    finally:
        profile.close()
    # Successful recreation proves deletion, rather than merely dropping a handle.
    replacement = appcontainer.Profile(tmp_path / "task").create()
    replacement.close()


def test_verification_env_drops_provider_and_python_paths(tmp_path):
    session = sandbox.Session(str(tmp_path), tmp_path / "scratch", writable_cwd=False)
    env = session.env({"USERPROFILE": "secret-profile", "APPDATA": "secret-appdata",
                       "PYTHONPATH": "secret-source", "PYTHONHOME": "secret-python",
                       "CODEX_HOME": "secret-auth", "CLAUDE_CONFIG_DIR": "secret-auth",
                       "CUSTOM_API_KEY": "secret", "SystemRoot": "C:/Windows"})
    assert "secret" not in str(env)
    assert env["USERPROFILE"] == str(session.tmp)
    assert env["SystemRoot"] == "C:/Windows"


@pytest.mark.skipif(os.name != "nt", reason="Windows API")
def test_spawn_refuses_missing_profile(tmp_path):
    with pytest.raises(sandbox.SandboxUnavailable, match="AppContainer"):
        sandbox.spawn(["python.exe"], cwd=str(tmp_path), env={}, stdout_fd=-1)


@pytest.mark.skipif(os.name != "nt", reason="Windows API")
def test_wrong_token_is_refused():
    with pytest.raises(appcontainer.AppContainerUnavailable, match="not an AppContainer"):
        appcontainer.verify_process(os.getpid(), "S-1-15-2-123")
