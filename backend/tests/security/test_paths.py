import os
import sys

import pytest

from orchestrator.security.paths import PathPolicyError, resolve_inside, validate_root


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "project"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.txt").write_text("a")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("s")
    return r


def test_inside_ok(root):
    assert resolve_inside("src/a.txt", [root], base=str(root)) == (root / "src" / "a.txt").resolve()
    assert resolve_inside(str(root / "src"), [root]) == (root / "src").resolve()
    assert resolve_inside(str(root), [root]) == root.resolve()


@pytest.mark.parametrize(
    "raw",
    [
        "../outside/secret.txt",
        "..\\outside\\secret.txt",
        "src/../../outside/secret.txt",
        "src\\..\\..\\outside",
    ],
)
def test_traversal_rejected(root, raw):
    with pytest.raises(PathPolicyError):
        resolve_inside(raw, [root], base=str(root))


def test_absolute_outside_rejected(root, tmp_path):
    with pytest.raises(PathPolicyError):
        resolve_inside(str(tmp_path / "outside" / "secret.txt"), [root])


def test_sibling_prefix_not_confused(root, tmp_path):
    evil = tmp_path / "project-evil"
    evil.mkdir()
    with pytest.raises(PathPolicyError):
        resolve_inside(str(evil), [root])


@pytest.mark.parametrize(
    "raw",
    ["\\\\server\\share\\x", "//server/share/x", "\\\\?\\C:\\Windows", "\\\\.\\pipe\\x", "src/a.txt:stream", "src\x00a", "C:relative"],
)
def test_unc_device_ads_nul_rejected(root, raw):
    with pytest.raises(PathPolicyError):
        resolve_inside(raw, [root], base=str(root))


def test_relative_without_base_rejected(root):
    with pytest.raises(PathPolicyError):
        resolve_inside("src", [root])


def test_symlink_escape_rejected(root, tmp_path):
    link = root / "link"
    try:
        os.symlink(tmp_path / "outside", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this machine")
    with pytest.raises(PathPolicyError):
        resolve_inside("link/secret.txt", [root], base=str(root))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_escape_rejected(root, tmp_path):
    import _winapi

    junction = root / "junction"
    _winapi.CreateJunction(str(tmp_path / "outside"), str(junction))
    assert (junction / "secret.txt").exists()
    with pytest.raises(PathPolicyError):
        resolve_inside("junction/secret.txt", [root], base=str(root))


def test_validate_root(tmp_path):
    assert validate_root(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(PathPolicyError):
        validate_root("relative/path")
    with pytest.raises(PathPolicyError):
        validate_root(str(tmp_path / "missing"))
    with pytest.raises(PathPolicyError):
        validate_root("\\\\server\\share")
