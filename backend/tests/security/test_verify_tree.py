"""Copy-in verification tree (N2): the sandbox identity is granted only on a disposable orchestrator-built copy.

Defensive invariants of the orchestrator's own construction. No sandboxed process creates a link to an outside
target in these tests; links that appear are planted by the test itself, at Medium, BEFORE the copy starts.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from orchestrator.scheduler import verification
from orchestrator.security import acl, appcontainer, sandbox, verify_tree

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the verification sandbox is Windows-only")


def _state(root: Path) -> dict[str, tuple]:
    """Every entry of `root`: kind, content hash, link count, reparse flag, DACL and label."""
    out = {}
    for p in [root, *sorted(root.rglob("*"))]:
        st = os.lstat(p)
        sec = acl.read_security(p)
        out[p.relative_to(root).as_posix()] = (
            stat.S_IFMT(st.st_mode), None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(),
            st.st_nlink, sandbox.is_reparse(st), sec["owner"], sec["protected"],
            tuple((a["type"], a["sid"], a["mask"], a["flags"]) for a in sec["aces"] or []), sandbox.read_label(p))
    return out


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "worktrees" / "task1"
    (src / "pkg" / "deep").mkdir(parents=True)
    (src / ".git").write_bytes(b"gitdir: synthetic\n")
    (src / "README.md").write_text("base\n")
    (src / "pkg" / "mod.py").write_text("value = 42\n")
    (src / "pkg" / "deep" / "data.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 17))  # spans several chunks
    (src / "empty").mkdir()
    return src


def _tree(source: Path, tmp_path: Path, **kw) -> verify_tree.VerificationTree:
    return verify_tree.VerificationTree(source, tmp_path / "sandbox" / "proj", **kw)


# ----------------------------------------------------------------------------------------------- construction


@windows_only
def test_copy_is_exact_single_linked_and_grant_exists_only_on_the_copy(source: Path, tmp_path: Path):
    before = _state(source)
    tree = _tree(source, tmp_path)
    path = tree.create()
    sid = appcontainer.task_sid(path)

    assert _state(source) == before, "the worktree (content, links, ACL, label) must be untouched"
    assert all(sid not in {ace[1] for ace in entry[6]} for entry in before.values())
    assert sandbox.read_label(source) is None and not sandbox.is_pinned(source)

    copied = _state(path)
    assert set(copied) == set(before)
    for rel, entry in copied.items():
        assert entry[1] == before[rel][1], rel  # same bytes
        assert entry[2] == 1 and not entry[3], rel  # one link, never a reparse point
        assert any(ace[1] == sid for ace in entry[6]), f"{rel} did not inherit the run identity"
        assert entry[7][0] == sandbox.LOW_RID, rel
    assert sandbox.is_pinned(path)
    # The run directory around the copy stays Medium and ungranted: it cannot be swapped by the command.
    for medium in (tree.base, tree.base.parent, tree.base.parent.parent):
        assert sandbox.read_label(medium) is None
        assert all(a["sid"] != sid for a in acl.read_security(medium)["aces"] or [])

    tree.destroy()
    assert not tree.base.exists() and list(tree.base.parent.iterdir()) == []
    assert _state(source) == before


@windows_only
def test_every_tree_gets_a_fresh_location_and_identity(source: Path, tmp_path: Path):
    first, second = _tree(source, tmp_path), _tree(source, tmp_path)
    try:
        a, b = first.create(), second.create()
        assert a != b
        assert len({appcontainer.task_sid(a), appcontainer.task_sid(b), appcontainer.task_sid(source)}) == 3
    finally:
        first.destroy()
        second.destroy()


@windows_only
def test_alternate_data_streams_and_attributes_are_not_copied(source: Path, tmp_path: Path):
    with open(str(source / "README.md") + ":hidden", "wb") as fh:
        fh.write(b"stream payload")
    os.chmod(source / "pkg" / "mod.py", stat.S_IREAD)
    tree = _tree(source, tmp_path)
    try:
        path = tree.create()
        assert (path / "README.md").read_text() == "base\n"
        with pytest.raises(OSError):
            open(str(path / "README.md") + ":hidden", "rb").close()
        assert os.access(path / "pkg" / "mod.py", os.W_OK)  # read-only attribute not carried: copy stays removable
        assert os.stat(path / "pkg" / "mod.py").st_mtime_ns == os.stat(source / "pkg" / "mod.py").st_mtime_ns
    finally:
        os.chmod(source / "pkg" / "mod.py", stat.S_IWRITE | stat.S_IREAD)
        tree.destroy()
    assert not tree.base.exists()


# ----------------------------------------------------------------------------------------------- refusals


def _assert_nothing_left(tree: verify_tree.VerificationTree) -> None:
    assert not tree.base.exists()
    runs = tree.base.parent
    assert not runs.exists() or list(runs.iterdir()) == []


@windows_only
def test_preexisting_hardlink_is_refused_and_nothing_is_built(source: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.link(outside, source / "pkg" / "alias.txt")  # planted by the test before any copy
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxTampered):
        tree.create()
    _assert_nothing_left(tree)
    assert outside.read_text() == "outside\n" and sandbox.read_label(outside) is None


@windows_only
def test_preexisting_junction_is_refused_and_nothing_is_built(source: Path, tmp_path: Path):
    import _winapi

    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n")
    _winapi.CreateJunction(str(outside), str(source / "pkg" / "jn"))  # planted by the test before any copy
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxTampered):
        tree.create()
    _assert_nothing_left(tree)
    assert sandbox.read_label(outside) is None and (outside / "secret.txt").read_text() == "secret\n"


@windows_only
def test_copy_reads_through_checked_handles_not_through_the_preflight(source: Path, tmp_path: Path, monkeypatch):
    """Even with the path scan disabled, a hardlinked or reparse source object can never enter the copy."""
    from orchestrator.gitops import git

    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    os.link(outside, source / "zz-alias.txt")
    monkeypatch.setattr(git, "assert_plain_worktree", lambda *a, **k: None)
    monkeypatch.setattr(verify_tree, "preflight", lambda *a, **k: None)
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxTampered, match="hardlinked"):
        tree.create()
    _assert_nothing_left(tree)

    os.unlink(source / "zz-alias.txt")
    import _winapi

    _winapi.CreateJunction(str(tmp_path), str(source / "zz-jn"))
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxTampered, match="link appeared"):
        tree.create()
    _assert_nothing_left(tree)


@windows_only
@pytest.mark.parametrize("limit", ["MAX_ENTRIES", "MAX_BYTES", "MAX_DEPTH"])
def test_bounds_fail_closed_and_clean_up(source: Path, tmp_path: Path, monkeypatch, limit: str):
    monkeypatch.setattr(verify_tree, limit, {"MAX_ENTRIES": 3, "MAX_BYTES": 1024, "MAX_DEPTH": 1}[limit])
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxUnavailable):
        tree.create()
    _assert_nothing_left(tree)


@windows_only
def test_deadline_or_cancellation_during_the_copy_stops_it_and_cleans_up(source: Path, tmp_path: Path):
    class Deadline(Exception):
        pass

    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise Deadline()

    tree = _tree(source, tmp_path, check=check)
    with pytest.raises(Deadline):
        tree.create()
    _assert_nothing_left(tree)
    with pytest.raises(sandbox.SandboxUnavailable, match="single-use"):
        tree.create()


@windows_only
def test_worktree_already_granted_to_the_run_identity_is_refused(source: Path, tmp_path: Path, monkeypatch):
    fixed = appcontainer.task_sid(tmp_path / "some-profile")
    monkeypatch.setattr(appcontainer, "task_sid", lambda path: fixed)
    appcontainer.grant_directory(source, fixed, writable=True)  # what a pre-copy-in version left on a worktree
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxUnavailable, match="carries a grant"):
        tree.create()
    _assert_nothing_left(tree)


@windows_only
def test_low_writable_runs_parent_is_refused(source: Path, tmp_path: Path):
    runs = tmp_path / "sandbox" / "proj" / "runs"
    runs.mkdir(parents=True)
    sandbox.label_low(runs)
    tree = _tree(source, tmp_path)
    with pytest.raises(sandbox.SandboxUnavailable, match="Low integrity"):
        tree.create()
    assert list(runs.iterdir()) == []


def test_session_runs_in_the_copy_and_never_in_the_worktree(tmp_path: Path):
    wt = tmp_path / "task"
    wt.mkdir()
    session = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=True)
    assert session.source == str(wt) and session.cwd != str(wt)
    assert Path(session.cwd) == session.root / "tree" and session.root.parent == tmp_path / "scratch" / "runs"
    # Scratch and caches are per command too: no directory survives into another run.
    assert session.root in session.tmp.parents and session.cache.parent == session.root
    other = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=True)
    assert other.cwd != session.cwd and other.cache != session.cache
    read_only = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=False)
    assert read_only._tree is None  # refused by prepare(); nothing is ever copied or granted


def test_session_refuses_to_prepare_anything_but_its_own_tree(tmp_path: Path, monkeypatch):
    wt = tmp_path / "task"
    wt.mkdir()
    monkeypatch.setattr(sandbox, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox.Session, "_check_data_dir", lambda self: None)
    session = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=True)
    session.cwd = str(wt)  # a caller redirecting the session at the real worktree
    with pytest.raises(sandbox.SandboxUnavailable, match="copy-in"):
        session.prepare()
    assert not (tmp_path / "scratch").exists()


def test_absolute_worktree_arguments_are_mapped_into_the_copy():
    m = verification._map_into_copy
    wt, copy = r"D:\wt\task1", r"D:\sbx\runs\ab\tree"
    assert m(r"D:\wt\task1\tests\t.py", wt, copy) == r"D:\sbx\runs\ab\tree\tests\t.py"
    assert m(r"d:\WT\TASK1", wt, copy) == copy
    assert m(r"D:\wt\task10\t.py", wt, copy) == r"D:\wt\task10\t.py"  # sibling with a common prefix
    assert m("tests/t.py", wt, copy) == "tests/t.py" and m("-q", wt, copy) == "-q"


# ----------------------------------------------------------------------------------------------- native end to end


@windows_only
async def test_native_command_writes_only_its_copy_and_the_worktree_is_untouched(tmp_path: Path):
    """Production path: real private runtime, AppContainer, Low token and jobs. The command makes ordinary
    writes in its working directory only."""
    from orchestrator.gitops import git, worktrees

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("base\n")
    (repo / "helper.py").write_text("value = 42\n")
    base = git.init_repo(repo)
    wt = Path(worktrees.create(repo, tmp_path / "worktrees" / "task1", "task1", base).path)
    assert sandbox.read_label(wt) is None, "a new worktree is no longer labelled for the sandbox"
    assert not any(a["sid"] and a["sid"].startswith("S-1-15-2-") for a in acl.read_security(wt)["aces"] or [])
    before = _state(wt)
    scratch = tmp_path / "worktrees" / "_sandbox" / "proj"

    code = ("import os, helper; print('cwd=' + os.getcwd()); print('helper', helper.value); "
            "open('artifact.txt', 'w').write('made by verification'); "
            "open('README.md', 'a').write('changed by verification'); "
            "print('copy-readme', open('README.md').read().count('changed'))")
    result = await verification.run_command([sys.executable, "-c", code], str(wt), 120, dict(os.environ),
                                            writable_cwd=True, sandbox_root=scratch, tmp_dir=tmp_path / "logs")
    assert result.passed, (result.error, result.output)
    assert "helper 42" in result.output and "copy-readme 1" in result.output
    assert f"cwd={wt}" in result.output, "the copy's path is reported as the worktree path"
    assert "runs" not in result.output

    assert _state(wt) == before, "content, links, ACLs and labels of the worktree are unchanged"
    assert not (wt / "artifact.txt").exists() and (wt / "README.md").read_text() == "base\n"
    assert list((scratch / "runs").iterdir()) == [], "copy, scratch and caches are removed after the command"
    assert [p.name for p in scratch.iterdir()] == ["runs"], "no per-task cache survives the command"
    git.assert_plain_worktree(str(wt), include_git=True)


@windows_only
async def test_native_command_cannot_write_the_real_worktree_by_absolute_path(tmp_path: Path):
    """The effective access of the task token on the real worktree: an ordinary create/append is denied."""
    from orchestrator.gitops import git, worktrees

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("base\n")
    base = git.init_repo(repo)
    wt = Path(worktrees.create(repo, tmp_path / "worktrees" / "task1", "task1", base).path)
    before = _state(wt)
    # Passed through the environment so that argument mapping does not rewrite it to the copy.
    code = ("import os\nroot = os.environ['PATHEXT'].split('|')[1]\n"
            "for name, mode in (('new.txt', 'w'), ('README.md', 'a')):\n"
            "    try:\n        open(os.path.join(root, name), mode).write('x'); print('ALLOWED', name)\n"
            "    except OSError as e:\n        print('denied', name, type(e).__name__)\n")
    env = dict(os.environ)
    env["PATHEXT"] = env.get("PATHEXT", ".EXE") + "|" + str(wt)
    result = await verification.run_command([sys.executable, "-c", code], str(wt), 120, env, writable_cwd=True,
                                            sandbox_root=tmp_path / "worktrees" / "_sandbox" / "proj",
                                            tmp_dir=tmp_path / "logs")
    assert result.exit_code == 0, (result.error, result.output)
    assert "ALLOWED" not in result.output and result.output.count("denied") == 2, result.output
    assert _state(wt) == before
