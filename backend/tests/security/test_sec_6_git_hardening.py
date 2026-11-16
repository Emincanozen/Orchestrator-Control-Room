"""SECURITY_REVIEW #6: orchestrator git calls must not execute repository-controlled code.

Every test plants something that would run a command (hook, fsmonitor, textconv, external diff, clean filter)
in a real temp repository, first proves with an *unhardened* git call that the plant works (control), and then
proves the orchestrator's git wrapper does not trigger it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.gitops import git as og

_BASE_ENV = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
_BASE_ENV.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def plain(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_BASE_ENV)
    if check:
        assert p.returncode == 0, p.stderr
    return p


def script(tmp: Path, name: str, marker: Path, body: str = "") -> str:
    """A POSIX sh script that records that it ran; returns its path (forward slashes)."""
    path = tmp / name
    path.write_bytes(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\n{body}\n'.encode())
    return path.as_posix()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    plain(r, "init", "-q", "-b", "main")
    plain(r, "config", "core.autocrlf", "false")
    (r / "a.dat").write_text("one\n")
    plain(r, "add", "-A")
    plain(r, "commit", "-q", "-m", "init")
    return r


def test_pre_commit_hook_and_planted_hooks_path_do_not_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "hook-marker"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 0\n'.encode())
    evil_hooks = tmp_path / "evil-hooks"
    evil_hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "post-commit", "prepare-commit-msg"):
        (evil_hooks / name).write_bytes(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 0\n'.encode())
    # control: a plain commit runs the hook
    (repo / "b.txt").write_text("x")
    plain(repo, "add", "-A")
    plain(repo, "commit", "-q", "-m", "control")
    assert marker.exists(), "control failed: the planted hook does not fire on this git"
    marker.unlink()

    plain(repo, "config", "core.hooksPath", evil_hooks.as_posix())
    (repo / "c.txt").write_text("y")
    sha = og.commit_all(repo, "checkpoint")
    assert sha is not None
    assert not marker.exists()
    assert og.rev_parse(repo) == sha


def test_planted_fsmonitor_does_not_run_on_status_or_diff(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "fsm-marker"
    cmd = script(tmp_path, "fsm.sh", marker, "printf '\0'")
    plain(repo, "config", "core.fsmonitor", f"sh {cmd}")
    (repo / "a.dat").write_text("two\n")
    plain(repo, "status", "--porcelain", check=False)
    control_fired = marker.exists()
    if marker.exists():
        marker.unlink()
    og.status_porcelain(repo)
    og.has_changes(repo)
    og.diff(repo)
    og.changed_files(repo, "HEAD")
    assert not marker.exists()
    # the control is informative only: some git builds ignore a non-builtin fsmonitor command on this platform
    print("fsmonitor control fired:", control_fired)


def test_textconv_and_external_diff_drivers_do_not_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "diff-marker"
    conv = script(tmp_path, "conv.sh", marker, 'cat "$1"')
    (repo / ".gitattributes").write_text("*.dat diff=evil\n")
    plain(repo, "config", "diff.evil.textconv", f"sh {conv}")
    plain(repo, "config", "diff.evil.command", f"sh {conv}")
    plain(repo, "config", "diff.external", f"sh {conv}")
    plain(repo, "add", "-A")
    plain(repo, "commit", "-q", "-m", "attrs")
    (repo / "a.dat").write_text("changed\n")
    plain(repo, "diff", check=False)
    assert marker.exists(), "control failed: the planted diff driver does not fire on this git"
    marker.unlink()
    out = og.diff(repo)
    assert "changed" in out
    og.diff(repo, "HEAD", stat=True)
    og.changed_files(repo, "HEAD")
    og.log_oneline(repo, "HEAD")
    assert not marker.exists()


def test_planted_clean_filter_does_not_run_on_add_status_commit(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "filter-marker"
    flt = script(tmp_path, "flt.sh", marker, "cat")
    (repo / ".gitattributes").write_text("*.dat filter=evil\n")
    plain(repo, "config", "filter.evil.clean", f"sh {flt}")
    plain(repo, "config", "filter.evil.smudge", f"sh {flt}")
    plain(repo, "config", "filter.evil.process", f"sh {flt}")
    plain(repo, "config", "filter.evil.required", "true")
    (repo / "a.dat").write_text("touched\n")
    plain(repo, "add", "-A", check=False)
    plain(repo, "reset", "-q", check=False)
    assert marker.exists(), "control failed: the planted filter does not fire on this git"
    marker.unlink()
    og.status_porcelain(repo)
    (repo / "new.dat").write_text("n\n")
    assert og.commit_all(repo, "ckpt") is not None
    assert not marker.exists()
    og.reset_hard(repo, "HEAD")
    assert not marker.exists()


def test_git_environment_is_allowlisted(monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(other))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "evil")
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("ORCH_SECRET_TOKEN", "s3cret")
    env = og._env()
    assert not [k for k in env if k.upper().startswith("GIT_") and k not in
                {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_PAGER", "GIT_EDITOR", "GIT_ATTR_NOSYSTEM"}]
    assert "ORCH_SECRET_TOKEN" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "PATH" in env
    # and behaviourally: the repo that cwd points at is used, not GIT_DIR
    assert og.is_repo(repo)
    assert og.toplevel(repo).replace("\\", "/").lower() == repo.as_posix().lower()


def test_hardening_flags_are_on_every_call() -> None:
    prefix = og.git_prefix()
    joined = " ".join(prefix)
    for needle in ("--no-pager", f"core.hooksPath={os.devnull}", "core.fsmonitor=false", "protocol.ext.allow=never"):
        assert needle in joined


def test_commit_uses_no_verify_and_fixed_identity(repo: Path) -> None:
    (repo / "z.txt").write_text("z")
    sha = og.commit_all(repo, "msg")
    p = plain(repo, "log", "-1", "--format=%an <%ae>|%s")
    assert p.stdout.strip() == "AI Orchestrator <orchestrator@localhost>|msg"
    assert sha == og.rev_parse(repo)
