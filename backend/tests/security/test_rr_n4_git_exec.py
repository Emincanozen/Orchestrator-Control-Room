"""Re-review N4: git paths that still executed repository-controlled programs, and git children outside the job.

Each test plants a program in a real temp repository's config, proves with a plain (unhardened) git call that
the plant fires on this git (control), then proves the orchestrator's wrapper does not run it."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from orchestrator.gitops import git as og
from orchestrator.security import worker_job

WIN = sys.platform == "win32"
_BASE_ENV = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
_BASE_ENV.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def plain(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_BASE_ENV)
    if check:
        assert p.returncode == 0, p.stderr
    return p


def planted_program(tmp: Path, marker: Path) -> str:
    """A program git can start directly (gpg.program is not run through a shell): .cmd on Windows."""
    if WIN:
        path = tmp / "evil.cmd"
        path.write_text(f'@echo ran>> "{marker}"\r\n@exit /b 1\r\n')
        return str(path)
    path = tmp / "evil.sh"
    path.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 1\n')
    path.chmod(0o755)
    return str(path)


def sh_script(tmp: Path, name: str, marker: Path) -> str:
    path = tmp / name
    path.write_bytes(f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\ncat "$1" 2>/dev/null\nexit 0\n'.encode())
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


def test_planted_gpg_program_does_not_run_on_commit_all(repo: Path, tmp_path: Path) -> None:
    """The reviewer's gpg_probe.py: commit.gpgSign=true + gpg.program=evil executed evil on commit_all."""
    marker = tmp_path / "gpg-marker"
    evil = planted_program(tmp_path, marker)
    plain(repo, "config", "commit.gpgSign", "true")
    plain(repo, "config", "gpg.program", evil)
    plain(repo, "config", "gpg.ssh.program", evil)
    plain(repo, "config", "gpg.x509.program", evil)
    plain(repo, "config", "tag.gpgSign", "true")
    plain(repo, "config", "log.showSignature", "true")
    (repo / "b.txt").write_text("x")
    plain(repo, "add", "-A")
    plain(repo, "commit", "-q", "-m", "control", check=False)
    assert marker.exists(), "control failed: the planted gpg.program does not fire on this git"
    marker.unlink()
    plain(repo, "reset", "-q", "--hard", "HEAD")

    (repo / "c.txt").write_text("y")
    sha = og.commit_all(repo, "checkpoint")
    assert sha is not None and og.rev_parse(repo) == sha
    og.log_oneline(repo, "HEAD~1..HEAD")  # log.showSignature=true would verify with gpg.program
    assert not marker.exists(), "the planted signing program ran"


def test_planted_merge_driver_and_diff_command_are_blanked(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "drv-marker"
    cmd = sh_script(tmp_path, "drv.sh", marker)
    (repo / ".gitattributes").write_text("*.dat merge=evil diff=evil\n")
    plain(repo, "config", "merge.evil.driver", f"sh {cmd} %A")
    plain(repo, "config", "diff.evil.command", f"sh {cmd}")
    plain(repo, "config", "diff.evil.textconv", f"sh {cmd}")
    plain(repo, "add", "-A")
    plain(repo, "commit", "-q", "-m", "attrs")
    (repo / "a.dat").write_text("two\n")
    plain(repo, "diff", check=False)  # control: diff.<driver>.command runs on a plain diff
    assert marker.exists(), "control failed: the planted diff driver does not fire on this git"
    marker.unlink()

    # Every git command the orchestrator runs on a worktree (status, diff, add, commit, log, reset/clean).
    og.status_porcelain(repo)
    og.diff(repo)
    og.diff(repo, "HEAD", stat=True)
    og.changed_files(repo, "HEAD")
    og.commit_all(repo, "checkpoint")
    og.log_oneline(repo, "HEAD~1..HEAD")
    og.reset_hard(repo, "HEAD~1")
    assert not marker.exists(), "a planted driver ran through the orchestrator's git wrapper"
    # The overrides name every configured driver key.
    overrides = og._filter_overrides(repo)
    for key in ("merge.evil.driver=", "diff.evil.command=", "diff.evil.textconv="):
        assert key in overrides


def test_hardening_prefix_neutralises_signing_and_programs() -> None:
    cfg = dict(og.HARDENING_CONFIG)
    assert cfg["commit.gpgSign"] == "false" and cfg["tag.gpgSign"] == "false"
    for key in ("gpg.program", "gpg.ssh.program", "gpg.x509.program", "diff.external", "core.alternateRefsCommand"):
        assert cfg[key] == ""
    assert cfg["sequence.editor"] == "true" and cfg["protocol.git.allow"] == "never"


def test_subcommand_must_be_a_known_literal(repo: Path) -> None:
    """argv[0] after the options is never caller-controlled (an unknown name could be a repository alias)."""
    plain(repo, "config", "alias.evil", "!echo pwned")
    with pytest.raises(ValueError):
        og.run_git(["evil"], repo)
    with pytest.raises(ValueError):
        og.run_git(["-c", "x.y=z", "evil"], repo)


def test_odd_driver_key_refuses_rather_than_running(repo: Path) -> None:
    plain(repo, "config", 'diff.a b.command', "sh -c true")  # subsection with a space
    with pytest.raises(og.GitError):
        og.status_porcelain(repo)


@pytest.mark.skipif(not WIN, reason="job objects are Windows-only")
def test_git_children_are_confined_in_the_worker_job(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """Every orchestrator git child (gitops, skills cat-file, knowledge ls-files) is assigned to the worker job
    before it runs, so anything it starts is refused by the API's worker-origin guard."""
    from orchestrator.knowledge import sources as ksrc

    real = worker_job.confine_spawned
    seen: list[tuple[int, object]] = []

    def spy(pid: int, *, suspended: bool) -> bool:
        ok = real(pid, suspended=suspended)
        seen.append((pid, worker_job.is_in_job(pid)))  # Popen still holds the handle: queryable even after exit
        return ok

    monkeypatch.setattr(worker_job, "confine_spawned", spy)
    og.status_porcelain(repo)
    og.commit_all(repo, "nothing")
    assert ksrc.is_git_repo(repo)
    assert ksrc.candidate_files(repo)[1] is True
    from orchestrator.skills import sources as ssrc

    head = og.rev_parse(repo)
    blobs = ssrc._cat_blobs(repo, [plain(repo, "rev-parse", f"{head}:a.dat").stdout.strip()], timeout=30)
    assert list(blobs.values()) == [(repo / "a.dat").read_bytes()]
    assert len(seen) >= 6, seen
    assert all(member is True for _pid, member in seen), seen


@pytest.mark.skipif(not WIN, reason="job objects are Windows-only")
def test_planted_program_started_by_git_is_a_worker(repo: Path, tmp_path: Path) -> None:
    """Defence in depth: even a program git does start (here: a diff driver through a plain `diff` invoked via
    the confined runner without overrides) is a job member."""
    marker = tmp_path / "pid-marker"
    probe = tmp_path / "probe.py"
    probe.write_text(f"import os; open(r'{marker}', 'w').write(str(os.getpid())); import time; time.sleep(2)\n")
    py = Path(sys.executable).as_posix()
    (repo / ".gitattributes").write_text("*.dat diff=evil\n")
    plain(repo, "config", "diff.evil.command", f'"{py}" "{probe.as_posix()}"')
    (repo / "a.dat").write_text("changed\n")
    out: dict = {}

    def go() -> None:
        out["r"] = og._run([*og.git_prefix(), "diff"], repo, timeout=30)

    t = threading.Thread(target=go)
    t.start()
    deadline = time.monotonic() + 15
    while not (marker.exists() and marker.read_text()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "control failed: the planted diff command did not run"
    pid = int(marker.read_text())
    assert worker_job.is_in_job(pid) is True
    assert worker_job.is_worker(pid) is True
    t.join(30)


@pytest.mark.skipif(not WIN, reason="job objects are Windows-only")
def test_run_confined_failure_kills_and_raises(tmp_path: Path, monkeypatch) -> None:
    started: list[int] = []

    def failing_assign(pid: int) -> None:
        started.append(pid)
        raise OSError(5, "simulated AssignProcessToJobObject failure")

    monkeypatch.setattr(worker_job, "_assign_windows", failing_assign)
    marker = tmp_path / "ran"
    with pytest.raises(OSError):
        worker_job.run_confined([sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"], cwd=str(tmp_path), timeout=30)
    assert started and not psutil.pid_exists(started[0]) or not _alive(started[0])
    assert not marker.exists(), "the process ran although it could not be confined"


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
