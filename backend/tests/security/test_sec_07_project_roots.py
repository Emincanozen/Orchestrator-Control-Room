"""Finding #7 (MEDIUM): project registration must refuse drive roots, profile/system directories and anything
overlapping the orchestrator data or worktrees directory. Inputs are the script-style attacks from the review."""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import REPO_ROOT
from orchestrator.domain.models import ProjectCreate
from orchestrator.security.paths import PathPolicyError, validate_root

WIN = os.name == "nt"


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v and os.path.isdir(v) else None


def _attack_inputs() -> list[str]:
    home = str(Path.home())
    out = [home, str(Path(home).parent), home + os.sep + ".." + os.sep + Path(home).name]
    if WIN:
        drive = os.environ.get("SYSTEMDRIVE", "C:")
        out += [drive + "\\", drive + "/", drive + "\\.", "\\\\?\\" + drive + "\\", "\\\\server\\share",
                "\\\\server\\share\\", "\\\\localhost\\c$", drive, drive + "Users", drive + "Windows"]
        for name in ("SYSTEMROOT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "APPDATA", "LOCALAPPDATA"):
            v = _env(name)
            if v:
                out += [v, v.replace("\\", "/"), v.upper(), v + "\\"]
        sysroot = _env("SYSTEMROOT")
        if sysroot:
            out += [sysroot + "\\System32", sysroot + "/System32/drivers"]
        pf = _env("PROGRAMFILES")
        if pf:
            out += [str(p) for p in list(Path(pf).iterdir())[:2] if p.is_dir()]
    else:
        out += ["/", "/usr", "/etc", "/usr/local/bin"]
    return out


@pytest.mark.parametrize("raw", _attack_inputs())
def test_attack_inputs_are_refused(raw):
    with pytest.raises(PathPolicyError):
        validate_root(raw)


def test_data_and_worktrees_dirs_overlap_is_refused(tmp_path):
    data, wt = tmp_path / "data", tmp_path / "data-worktrees"
    for d in (data / "logs", wt / "task1"):
        d.mkdir(parents=True)
    for raw in (data, data / "logs", wt, wt / "task1", tmp_path):
        with pytest.raises(PathPolicyError):
            validate_root(str(raw), protected=[data, wt])
    other = tmp_path / "proj"
    other.mkdir()
    assert validate_root(str(other), protected=[data, wt]) == other.resolve()


def test_ordinary_project_dirs_are_allowed(tmp_path):
    assert validate_root(str(tmp_path)) == tmp_path.resolve()  # under %LOCALAPPDATA%\Temp: fine
    # The orchestrator's own checkout is NOT an ordinary project any more (re-review N3, architect decision):
    # a worker editing config/policies.yaml could switch on claude_shell / auto_approve.
    with pytest.raises(PathPolicyError):
        validate_root(str(REPO_ROOT))


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_junction_to_the_profile_is_refused(tmp_path):
    link = tmp_path / "harmless"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(Path.home())], capture_output=True)
    if r.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(PathPolicyError):
        validate_root(str(link))


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_junction_to_the_data_dir_is_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    link = tmp_path / "harmless"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(data)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(PathPolicyError):
        validate_root(str(link), protected=[data])


def _short_name(path: str) -> str | None:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
    return buf.value if n and buf.value.lower() != path.lower() else None


@pytest.mark.skipif(not WIN, reason="8.3 names are Windows-only")
def test_short_8dot3_names_are_resolved_before_the_check():
    candidates = [v for v in (_env("PROGRAMFILES"), _env("PROGRAMFILES(X86)"), str(Path.home())) if v]
    shorts = [s for s in (_short_name(c) for c in candidates) if s]
    if not shorts:
        pytest.skip("8.3 short names are disabled on this volume")
    for s in shorts:
        with pytest.raises(PathPolicyError):
            validate_root(s)


# --------------------------------------------------------------------------- through ProjectService


async def test_service_refuses_dirs_around_the_data_dir_and_keeps_in_place(h, tmp_path):
    paths = h.ctx.paths
    for raw in (paths.data_dir, paths.worktrees_dir, paths.logs_dir, paths.data_dir.parent):
        with pytest.raises(PathPolicyError):
            await h.projects.create(ProjectCreate(name="x", path=str(raw)))
    plain = tmp_path / "plain"
    plain.mkdir()
    p = await h.projects.create(ProjectCreate(name="plain", path=str(plain)))
    assert p.isolation == "in_place" and not p.is_git  # existing behaviour for non-git dirs
