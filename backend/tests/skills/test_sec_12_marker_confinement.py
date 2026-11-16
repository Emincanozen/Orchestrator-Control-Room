"""SECURITY_REVIEW #12: the on-disk marker's file list is untrusted; reinstall/removal must stay inside the skill dir."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.skills import artifacts


def _src(tmp: Path) -> Path:
    src = tmp / "src"
    (src / "sub").mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text("---\nname: demo\n---\nhi\n")
    (src / "sub" / "a.txt").write_text("a")
    return src


def _install(tmp: Path) -> tuple[Path, Path]:
    project = tmp / "proj"
    project.mkdir()
    art = artifacts.install_skill_dir(_src(tmp), str(project), "claude", "demo", "skill-1", "abc123")
    return project, Path(art["path"])


def _tamper(target: Path, extra: list[str]) -> None:
    marker = target / artifacts.MARKER
    data = json.loads(marker.read_text())
    data["files"] = [*data["files"], *extra]
    marker.write_text(json.dumps(data))


def test_reinstall_with_tampered_marker_does_not_delete_outside_files(tmp_path: Path) -> None:
    project, target = _install(tmp_path)
    victim = tmp_path / "outside-secret.txt"
    victim.write_text("keep me")
    in_project_victim = project / "precious.txt"
    in_project_victim.write_text("keep me too")
    sibling = target.parent / "other-skill.txt"
    sibling.write_text("sibling")
    _tamper(target, [
        "../../../outside-secret.txt", "../other-skill.txt", "../../../precious.txt", "sub/../../other-skill.txt",
        str(victim), victim.as_posix(), "/etc/passwd", "C:/Windows/win.ini", "C:evil.txt", "..\\..\\outside-secret.txt",
        "", ".", 42, None,
    ])
    new = artifacts.install_skill_dir(_src(tmp_path), str(project), "claude", "demo", "skill-1", "def456")
    assert victim.read_text() == "keep me"
    assert in_project_victim.read_text() == "keep me too"
    assert sibling.read_text() == "sibling"
    assert (Path(new["path"]) / "SKILL.md").is_file()  # reinstall itself still works


def test_remove_artifact_skips_unsafe_entries_and_reports_them(tmp_path: Path) -> None:
    project, target = _install(tmp_path)
    victim = tmp_path / "v.txt"
    victim.write_text("x")
    res = artifacts.remove_artifact({"type": "skill_dir", "path": str(target), "files": ["SKILL.md", "sub/a.txt", "../../../v.txt", str(victim)], "created_dirs": []})
    assert victim.exists()
    assert not (target / "SKILL.md").exists()
    assert any("v.txt" in x for x in res["left"])


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only; symlink case is below")
def test_junction_inside_skill_dir_pointing_outside_is_not_followed(tmp_path: Path) -> None:
    project, target = _install(tmp_path)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "important.txt").write_text("keep")
    link = target / "linked"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot create a junction: {r.stderr}")
    _tamper(target, ["linked/important.txt"])
    artifacts.remove_artifact({"type": "skill_dir", "path": str(target), "files": json.loads((target / artifacts.MARKER).read_text())["files"], "created_dirs": []})
    assert (outside / "important.txt").read_text() == "keep"


def test_symlink_dir_escape_is_not_followed(tmp_path: Path) -> None:
    project, target = _install(tmp_path)
    outside = tmp_path / "outside-dir2"
    outside.mkdir()
    (outside / "important.txt").write_text("keep")
    try:
        os.symlink(outside, target / "lnk", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted")
    artifacts.remove_artifact({"type": "skill_dir", "path": str(target), "files": ["lnk/important.txt"], "created_dirs": []})
    assert (outside / "important.txt").read_text() == "keep"


def test_confined_file_accepts_normal_entries(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    assert artifacts.confined_file(target, "a/b.txt") == target / "a" / "b.txt"
    for bad in ["../x", "a/../../x", "/abs", "C:/x", "a\\b", "", "a//b", "./a", None, 3]:
        assert artifacts.confined_file(target, bad) is None, bad
