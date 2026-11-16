"""Copy-in review follow-up (MED-1, LOW-2..LOW-5): no-follow handle-based cleanup, residue vs tamper, sweeps,
legacy worktree marks, handle-relative copy construction, argument/output mapping and package-wide ACEs.

What a sandboxed command could leave in its run directory is planted by the test itself (Medium, same user),
after the copy was built: read-only files, deep trees, junctions and hardlinks to outside targets, deny ACEs.
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from pathlib import Path

import pytest

from orchestrator.scheduler import verification
from orchestrator.security import acl, sandbox, verify_tree

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the verification sandbox is Windows-only")


@pytest.fixture(autouse=True)
def _fresh_sweep_state():
    """Residue scan schedule and backoff are process-wide; every test starts from a clean schedule."""
    verify_tree._BACKOFF.clear()
    verify_tree._RESIDUE_NEXT_SCAN.clear()
    yield
    verify_tree._BACKOFF.clear()
    verify_tree._RESIDUE_NEXT_SCAN.clear()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "worktrees" / "task1"
    (src / "pkg").mkdir(parents=True)
    (src / ".git").write_bytes(b"gitdir: synthetic\n")
    (src / "README.md").write_text("base\n")
    (src / "pkg" / "mod.py").write_text("value = 42\n")
    return src


def _tree(source: Path, tmp_path: Path, **kw) -> verify_tree.VerificationTree:
    return verify_tree.VerificationTree(source, tmp_path / "sandbox" / "proj", **kw)


def _set_dacl(path: Path, sddl: str) -> None:
    h = verify_tree._open_path(str(path), verify_tree._READ_CONTROL | verify_tree._WRITE_DAC, 7)
    try:
        verify_tree._set_dacl(h, sddl)
    finally:
        verify_tree._close(h)


def _deep(root: Path, depth: int) -> Path:
    """A directory chain longer than MAX_PATH, created through handles (as a command could)."""
    parent = verify_tree._open_path(str(root), verify_tree._FILE_LIST_DIRECTORY, 7)
    handles = [parent]
    try:
        for _ in range(depth):
            handles.append(verify_tree.VerificationTree._create_dir(handles[-1], "d" * 40))
    finally:
        for h in handles:
            verify_tree._close(h)
    return root


# ----------------------------------------------------------------------------------------------- MED-1 cleanup


@windows_only
def test_destroy_removes_readonly_deep_links_and_hardlinks_without_touching_targets(source: Path, tmp_path: Path):
    import _winapi

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("keep\n")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n")
    os.chmod(outside_file, stat.S_IREAD)
    tree = _tree(source, tmp_path)
    copy = tree.create()
    scratch = tree.base / "tmp" / "x"
    scratch.mkdir(parents=True)
    ro = scratch / "readonly.txt"
    ro.write_text("hostile")
    os.chmod(ro, stat.S_IREAD)
    os.chmod(copy / "pkg" / "mod.py", stat.S_IREAD)  # an ordinary read-only file (e.g. git objects)
    ro_dir = scratch / "rodir"
    ro_dir.mkdir()
    (ro_dir / "f").write_text("f")
    os.chmod(ro_dir, stat.S_IREAD)  # FILE_ATTRIBUTE_READONLY on a directory
    _winapi.CreateJunction(str(outside_dir), str(scratch / "jn"))
    os.symlink(outside_file, scratch / "ln") if _can_symlink(tmp_path) else None
    os.link(outside_file, scratch / "hard")
    _deep(scratch, 12)  # > 500 characters below scratch

    tree.destroy()

    assert not tree.base.exists() and list(tree.base.parent.iterdir()) == []
    assert (outside_dir / "keep.txt").read_text() == "keep\n"
    assert outside_file.read_text() == "outside\n"
    assert not os.access(outside_file, os.W_OK), "the hardlink target keeps its read-only attribute"
    assert os.stat(outside_file).st_nlink == 1
    os.chmod(outside_file, stat.S_IWRITE | stat.S_IREAD)


def _can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink(tmp_path, tmp_path / "probe-link", target_is_directory=True)
    except OSError:
        return False
    os.rmdir(tmp_path / "probe-link")
    return True


@windows_only
def test_deny_ace_set_by_a_command_is_reset_by_the_owner_and_removed(source: Path, tmp_path: Path):
    tree = _tree(source, tmp_path)
    tree.create()
    hidden = tree.base / "tmp" / "x" / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "inner.txt").write_text("inner")
    user = acl.current_user_sid()
    _set_dacl(hidden, f"D:P(D;OICI;FA;;;{user})")  # what WRITE_DAC on scratch allows a command to do
    with pytest.raises(PermissionError):
        os.listdir(hidden)
    tree.destroy()
    assert not tree.base.exists()


@windows_only
def test_extended_disposition_fallback_clears_readonly_through_the_handle(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_tree, "_disposition_ex", lambda h: False)
    tree = _tree(source, tmp_path)
    copy = tree.create()
    os.chmod(copy / "README.md", stat.S_IREAD)
    tree.destroy()
    assert not tree.base.exists()


@windows_only
def test_residue_is_retained_not_tamper_and_a_later_sweep_removes_it(source: Path, tmp_path: Path):
    tree = _tree(source, tmp_path)
    copy = tree.create()
    # An open handle without delete sharing (a straggler, an indexer) blocks removal of one file.
    holder = verify_tree._open_path(str(copy / "README.md"), 0x80000000, 0x1)
    try:
        with pytest.raises(verify_tree.ResidueRetained):
            tree.destroy()
        residue = tree.sandbox_root / verify_tree.RESIDUE_DIR / tree.base.name
        # Moved into residue/ when NTFS allows renaming a directory with an open descendant, else left in runs/.
        assert tree.residue and (tree.base.exists() or residue.exists()) and not verify_tree.is_live(tree.base)
    finally:
        verify_tree._close(holder)
    assert verify_tree.sweep(tree.sandbox_root) == []
    assert not tree.base.exists() and not residue.exists()


@windows_only
def test_session_finish_records_residue_without_a_security_finding(tmp_path: Path, monkeypatch):
    wt = tmp_path / "task"
    wt.mkdir()
    session = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=True)

    def residue():
        raise verify_tree.ResidueRetained(None, "1 entries could not be removed")

    monkeypatch.setattr(session._tree, "destroy", residue)
    assert session.finish() == [] and session.residue and session._finished

    other = sandbox.Session(str(wt), tmp_path / "scratch", writable_cwd=True)

    def replaced():
        raise OSError("the run directory is no longer the directory the orchestrator created")

    monkeypatch.setattr(other._tree, "destroy", replaced)
    assert any("cannot remove the verification tree" in t for t in other.finish())


@windows_only
def test_sweep_removes_dead_runs_and_legacy_dirs_but_never_a_live_session(source: Path, tmp_path: Path):
    root = tmp_path / "sandbox" / "proj"
    live = _tree(source, tmp_path)
    live.create()
    dead = root / "runs" / "deadbeefdeadbeef"
    (dead / "tree" / "sub").mkdir(parents=True)
    (dead / "tree" / "sub" / "f.txt").write_text("x")
    os.chmod(dead / "tree" / "sub" / "f.txt", stat.S_IREAD)
    for legacy in ("tmp", "cache-ai-orchestrator-v1-0123"):
        (root / legacy / "inner").mkdir(parents=True)
        (root / legacy / "inner" / "x.bin").write_bytes(b"x")
    (root / "keep-me").mkdir()

    assert verify_tree.sweep(root) == []
    assert not dead.exists() and not (root / "tmp").exists() and not (root / "cache-ai-orchestrator-v1-0123").exists()
    assert live.path.exists() and (live.path / "README.md").read_text() == "base\n"
    assert (root / "keep-me").exists(), "only run dirs and the legacy layout are swept"
    live.destroy()
    assert not live.base.exists()


@windows_only
def test_sweep_all_once_covers_every_project_once(tmp_path: Path):
    sandbox_dir = tmp_path / "worktrees" / "_sandbox"
    for project in ("a", "b"):
        (sandbox_dir / project / "runs" / "0011223344556677" / "tree").mkdir(parents=True)
    assert verify_tree.sweep_all_once(sandbox_dir) == []
    assert all(list((sandbox_dir / p / "runs").iterdir()) == [] for p in ("a", "b"))
    (sandbox_dir / "a" / "runs" / "8899aabbccddeeff").mkdir()
    verify_tree.sweep_all_once(sandbox_dir)
    assert (sandbox_dir / "a" / "runs" / "8899aabbccddeeff").exists(), "startup sweep runs once per process"


# ----------------------------------------------------------------------------------------------- LOW-3 copy


@windows_only
def test_copy_creates_and_stamps_destination_entries_only_through_held_handles(source: Path, tmp_path: Path,
                                                                              monkeypatch):
    tree = _tree(source, tmp_path)
    real_mkdir, real_open = os.mkdir, os.open
    dest = str(tree.path)

    def no_path_mkdir(path, *a, **k):
        if str(path).startswith(dest):
            raise AssertionError(f"copy created a directory by path: {path}")
        return real_mkdir(path, *a, **k)

    def no_path_open(path, flags, *a, **k):
        if str(path).startswith(dest):
            raise AssertionError(f"copy opened a destination by path: {path}")
        return real_open(path, flags, *a, **k)

    def no_utime(*a, **k):
        raise AssertionError("copy set times by path")

    monkeypatch.setattr(os, "mkdir", no_path_mkdir)
    monkeypatch.setattr(os, "open", no_path_open)
    monkeypatch.setattr(os, "utime", no_utime)
    path = tree.create()
    monkeypatch.undo()
    try:
        assert (path / "pkg" / "mod.py").read_text() == "value = 42\n"
        assert os.stat(path / "README.md").st_mtime_ns == os.stat(source / "README.md").st_mtime_ns
    finally:
        tree.destroy()


@windows_only
def test_deadline_is_checked_inside_a_large_file_copy(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_tree, "_CHUNK", 1024)
    (source / "big.bin").write_bytes(os.urandom(256 * 1024))

    class Deadline(Exception):
        pass

    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls > 40:
            raise Deadline()

    tree = _tree(source, tmp_path, check=check)
    with pytest.raises(Deadline):
        tree.create()
    assert not tree.base.exists()


@windows_only
def test_preflight_checks_the_deadline_while_scanning(tmp_path: Path):
    for i in range(200):
        (tmp_path / f"f{i}").write_text("x")
    calls = []
    verify_tree.preflight(tmp_path, check=lambda: calls.append(1))
    assert len(calls) >= 3


# ----------------------------------------------------------------------------------------------- LOW-5 package ACEs


def test_package_wide_write_aces_are_detected():
    aces = [{"type": 0, "sid": "S-1-15-2-1", "mask": 0x1200A9, "inherited": True, "flags": 0x13},
            {"type": 0, "sid": "S-1-15-2-2", "mask": 0x1301BF, "inherited": True, "flags": 0x13},
            {"type": 0, "sid": "S-1-15-2-1", "mask": 0x10000000, "inherited": True, "flags": 0x13},
            {"type": 1, "sid": "S-1-15-2-1", "mask": 0x1F01FF, "inherited": False, "flags": 0}]
    assert [a["mask"] for a in verify_tree.package_write_aces(aces)] == [0x1301BF, 0x10000000]
    assert verify_tree.package_write_aces(None), "a NULL DACL lets everybody write"
    assert verify_tree.package_write_aces([aces[0], aces[3]]) == []


@windows_only
def test_runs_is_protected_from_package_wide_grants_inherited_from_above(source: Path, tmp_path: Path):
    project = tmp_path / "sandbox" / "proj"
    runs = project / "runs"
    runs.mkdir(parents=True)
    user = acl.current_user_sid()
    # What a permissive drive root hands down: ALL APPLICATION PACKAGES may write.
    _set_dacl(project, f"D:(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;AC)")
    _set_dacl(runs, f"D:AI(A;OICI;FA;;;{user})(A;OICIID;0x1301bf;;;AC)")
    assert verify_tree.package_write_aces(acl.read_security(runs)["aces"])
    tree = _tree(source, tmp_path)
    tree.create()
    try:
        sec = acl.read_security(runs)
        assert sec["protected"] and not verify_tree.package_write_aces(sec["aces"])
        for p in (tree.base, tree.path, tree.path / "pkg" / "mod.py"):
            assert not verify_tree.package_write_aces(acl.read_security(p)["aces"]), p
    finally:
        tree.destroy()


# ----------------------------------------------------------------------------------------------- LOW-4 mapping


def test_option_values_separators_and_case_are_mapped_into_the_copy():
    m = verification._map_into_copy
    wt, copy = r"D:\wt\task1", r"D:\sbx\runs\ab\tree"
    assert m(r"--rootdir=D:\wt\task1", wt, copy) == r"--rootdir=D:\sbx\runs\ab\tree"
    assert m(r"--cov=d:/WT/task1/pkg", wt, copy) == r"--cov=D:\sbx\runs\ab\tree\pkg"
    assert m("D:/wt/task1/tests/t.py", wt, copy) == r"D:\sbx\runs\ab\tree\tests\t.py"
    assert m(r"-o=D:\wt\task1\x", wt, copy) == r"-o=D:\sbx\runs\ab\tree\x"
    assert m(r"--x=D:\wt\task10\t.py", wt, copy) == r"--x=D:\wt\task10\t.py"  # sibling with a common prefix
    assert m(r"name=D:\wt\task1", wt, copy) == r"name=D:\wt\task1"  # not an option
    assert m("--flag=", wt, copy) == "--flag=" and m("=", wt, copy) == "="


def test_output_maps_copy_and_scratch_paths_case_insensitively_for_both_separators():
    run = r"D:\sbx\runs\0123"
    mapping = [(run + r"\tree", r"D:\wt\task1"), (run + r"\tmp\aa", "<sandbox-tmp>"),
               (run + r"\cache-x", "<sandbox-cache>"), (run, "<sandbox-run>")]
    text = ("at D:\\SBX\\runs\\0123\\tree\\a.py and d:/sbx/runs/0123/tree/b.py, "
            "json \"D:\\\\sbx\\\\runs\\\\0123\\\\tree\\\\c.py\" tmp D:\\sbx\\runs\\0123\\tmp\\aa\\pytest-1 "
            "cache D:/sbx/runs/0123/cache-x/npm run D:\\sbx\\runs\\0123\\other")
    out = verification._map_output(text, mapping)
    assert "runs" not in out and "0123" not in out
    assert out.count(r"D:\wt\task1") == 3
    assert "<sandbox-tmp>\\pytest-1" in out and "<sandbox-cache>/npm" in out and "<sandbox-run>\\other" in out


# ----------------------------------------------------------------------------------------------- LOW-2 worktrees


def _legacy_worktree(tmp_path: Path, monkeypatch, *, dirty: bool):
    from orchestrator.gitops import git, worktrees
    from orchestrator.security import appcontainer

    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("base\n")
    base = git.init_repo(repo)
    wt_path = tmp_path / "worktrees" / "task1"
    with monkeypatch.context() as m:
        m.setattr(worktrees, "_pin_root", lambda p: None)  # what an old version did before pinning
        wt = Path(worktrees.create(repo, wt_path, "task1", base).path)
    appcontainer.grant_directory(wt, appcontainer.task_sid(wt), writable=True)
    sandbox.label_low(wt)
    sandbox.pin_directory(wt)
    (wt / "work.txt").write_text("committed work\n")
    git.commit_all(wt, "work")
    if dirty:
        (wt / "uncommitted.txt").write_text("not yet committed\n")
    return repo, wt, base


@windows_only
def test_legacy_labelled_worktree_is_recreated_from_its_branch(tmp_path: Path, monkeypatch):
    from orchestrator.gitops import git, worktrees

    repo, wt, base = _legacy_worktree(tmp_path, monkeypatch, dirty=False)
    head = git.rev_parse(wt)
    assert worktrees.legacy_sandbox_marks(wt)
    info = worktrees.create(repo, wt, "task1", base)
    assert info.created and info.head_commit == head
    assert worktrees.legacy_sandbox_marks(wt) == []
    assert sandbox.read_label(wt) is None and sandbox.read_label(wt / "work.txt") is None
    assert (wt / "work.txt").read_text() == "committed work\n"
    assert not any((a["sid"] or "").startswith("S-1-15-2-") for a in acl.read_security(wt / "work.txt")["aces"])
    # The old tree is set aside, not deleted, and no longer points at the reused git admin dir.
    (aside,) = list((wt.parent / worktrees.LEGACY_DIR).iterdir())
    assert (aside / "work.txt").read_text() == "committed work\n"
    assert not (aside / ".git").exists() and len(list(aside.glob(".git.orchestrator-detached-*"))) == 1
    assert git.rev_parse(wt) == head and not git.has_changes(wt)


@windows_only
def test_legacy_labelled_worktree_with_uncommitted_changes_is_refused(tmp_path: Path, monkeypatch):
    from orchestrator.gitops import git, worktrees

    repo, wt, base = _legacy_worktree(tmp_path, monkeypatch, dirty=True)
    with pytest.raises(git.UnsafeWorktree, match="uncommitted"):
        worktrees.create(repo, wt, "task1", base)
    assert (wt / "uncommitted.txt").read_text() == "not yet committed\n"


@windows_only
def test_legacy_worktree_with_detached_head_is_refused_and_left_in_place(tmp_path: Path, monkeypatch):
    from orchestrator.gitops import git, worktrees

    repo, wt, base = _legacy_worktree(tmp_path, monkeypatch, dirty=False)
    admin = repo / ".git" / "worktrees" / "task1"
    (admin / "HEAD").write_text(git.rev_parse(wt) + "\n")  # detached at the same commit
    with pytest.raises(git.UnsafeWorktree, match="HEAD is not the tip"):
        worktrees.create(repo, wt, "task1", base)
    assert (wt / "work.txt").exists() and not (wt.parent / worktrees.LEGACY_DIR).exists()


@windows_only
def test_legacy_worktree_with_a_nested_repository_is_refused(tmp_path: Path, monkeypatch):
    from orchestrator.gitops import git, worktrees

    repo, wt, base = _legacy_worktree(tmp_path, monkeypatch, dirty=False)
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()  # its own uncommitted work is invisible to a submodule-blind status
    monkeypatch.setattr(git, "has_changes", lambda path: False)
    with pytest.raises(git.UnsafeWorktree, match="nested repository"):
        worktrees.create(repo, wt, "task1", base)
    assert (nested / ".git").is_dir()


@windows_only
def test_inherited_package_ace_and_label_are_not_legacy_marks(tmp_path: Path):
    from orchestrator.gitops import worktrees
    from orchestrator.security import appcontainer

    parent = tmp_path / "permissive"
    parent.mkdir()
    sid = appcontainer.task_sid(parent)
    appcontainer.grant_directory(parent, sid, writable=True)  # inheritable (OICI) on an ancestor
    child = parent / "task1"
    child.mkdir()
    assert any(a["sid"] == sid and a["inherited"] for a in acl.read_security(child)["aces"])
    assert worktrees.legacy_sandbox_marks(child) == [], "an inherited ACE would recreate the worktree forever"
    appcontainer.grant_directory(child, appcontainer.task_sid(child), writable=True)
    assert worktrees.legacy_sandbox_marks(child), "an explicit package ACE on the root is a legacy mark"


# ----------------------------------------------------------------------------------- review of 65296a0: sweeps


def _dead_run(root: Path, name: str = "deadbeefdeadbeef") -> Path:
    dead = root / "runs" / name
    (dead / "tree" / "sub").mkdir(parents=True)
    (dead / "tree" / "sub" / "f.txt").write_text("x")
    return dead


@windows_only
def test_sweep_skips_a_low_labelled_project_root(tmp_path: Path):
    """MED-A: below a legacy Low-labelled root a Low process could swap runs/ for a junction; never sweep it."""
    root = tmp_path / "sandbox" / "proj"
    dead = _dead_run(root)
    sandbox.label_low(root)
    retained = verify_tree.sweep(root)
    assert retained and "Low integrity label" in retained[0]
    assert dead.exists()
    assert verify_tree.sweep_all(root.parent) and dead.exists()


@windows_only
def test_sweep_skips_a_root_granting_package_write(tmp_path: Path):
    root = tmp_path / "sandbox" / "proj"
    dead = _dead_run(root)
    user = acl.current_user_sid()
    _set_dacl(root, f"D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;;0x1301bf;;;AC)")
    assert "write grant for S-1-15-2-1" in verify_tree.sweep(root)[0]
    assert dead.exists()


@windows_only
def test_sweep_holds_root_and_runs_and_never_removes_by_path(tmp_path: Path, monkeypatch):
    """MED-A: root and runs/ cannot be renamed/replaced while the sweep enumerates and removes below them."""
    import _winapi

    root = tmp_path / "sandbox" / "proj"
    dead = _dead_run(root)
    outside = tmp_path / "outside"
    (outside / dead.name / "tree").mkdir(parents=True)
    (outside / dead.name / "tree" / "precious.txt").write_text("keep")
    swaps = []
    real_list = verify_tree._list_names

    def swap_while_listing(h, *a, **k):
        names = real_list(h, *a, **k)
        # 1st listing: the root (held); 2nd: runs/ (held). Each is what the sweep is working below.
        victims = [root, root / "runs"]
        if len(swaps) < 2:
            victim = victims[len(swaps)]
            try:
                os.rename(victim, str(victim) + "-old")
            except OSError as exc:
                swaps.append(exc.winerror)
            else:  # pragma: no cover - the red case: put a junction where runs/ was
                swaps.append("renamed")
                _winapi.CreateJunction(str(outside), str(victim))
        return names

    def no_path_removal(*a, **k):
        raise AssertionError("sweep removed something by path")

    monkeypatch.setattr(verify_tree, "_list_names", swap_while_listing)
    monkeypatch.setattr(verify_tree, "remove_path", no_path_removal)
    monkeypatch.setattr(sandbox, "remove_tree", no_path_removal)
    assert verify_tree.sweep(root) == []
    assert len(swaps) == 2 and "renamed" not in swaps, swaps
    assert not dead.exists()
    assert (outside / dead.name / "tree" / "precious.txt").read_text() == "keep"


@windows_only
def test_sweep_never_follows_a_runs_junction(tmp_path: Path):
    import _winapi

    root = tmp_path / "sandbox" / "proj"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "0011223344556677").mkdir(parents=True)
    (outside / "0011223344556677" / "precious.txt").write_text("keep")
    _winapi.CreateJunction(str(outside), str(root / "runs"))
    verify_tree.sweep(root)
    verify_tree.sweep_all(root.parent)
    assert (outside / "0011223344556677" / "precious.txt").read_text() == "keep"


def _hostile_dir(base: Path, name: str = "hidden") -> Path:
    hidden = base / "tmp" / "x" / name
    hidden.mkdir(parents=True)
    (hidden / "inner.txt").write_text("inner")
    _set_dacl(hidden, f"D:P(D;OICI;FA;;;{acl.current_user_sid()})")
    return hidden


@windows_only
def test_permanent_residue_is_quarantined_and_not_retried_by_every_command(source: Path, tmp_path: Path,
                                                                           monkeypatch):
    """MED-B: residue a command made unremovable (simulated: the owner reset is refused, as with an OWNER RIGHTS
    deny) moves to residue/ and costs later per-command sweeps nothing."""
    monkeypatch.setattr(verify_tree, "_reset_dacl", lambda *a: False)
    tree = _tree(source, tmp_path)
    tree.create()
    _hostile_dir(tree.base)
    with pytest.raises(verify_tree.ResidueRetained, match="moved to residue"):
        tree.destroy()
    root = tree.sandbox_root
    quarantined = root / verify_tree.RESIDUE_DIR / tree.base.name
    assert not tree.base.exists() and quarantined.is_dir()
    sec = acl.read_security(root / verify_tree.RESIDUE_DIR)
    assert sec["protected"] and not verify_tree.package_write_aces(sec["aces"])

    calls = []
    real_remove_below = verify_tree.remove_below
    monkeypatch.setattr(verify_tree, "remove_below", lambda *a, **k: calls.append(a[1]) or real_remove_below(*a, **k))
    assert verify_tree.sweep(root), "the first residue scan retries once and reports"
    first = len(calls)
    assert first >= 1
    for _ in range(5):  # later commands: residue/ not due, nothing retried
        assert verify_tree.sweep(root) == []
    assert len(calls) == first
    verify_tree._RESIDUE_NEXT_SCAN.clear()  # scan due again, but this object backs off
    assert verify_tree.sweep(root) == [] and len(calls) == first
    fid = next(iter(verify_tree._BACKOFF))
    assert verify_tree._BACKOFF[fid][0] == 1

    monkeypatch.undo()  # the owner reset works again: the low-frequency sweep removes it
    verify_tree._RESIDUE_NEXT_SCAN.clear()
    verify_tree._BACKOFF.clear()
    assert verify_tree.sweep(root) == []
    assert list((root / verify_tree.RESIDUE_DIR).iterdir()) == [] and list((root / "runs").iterdir()) == []


@windows_only
def test_destroy_is_bounded_by_a_deadline(source: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(verify_tree, "DESTROY_SECONDS", -1.0)
    tree = _tree(source, tmp_path)
    tree.create()
    with pytest.raises(verify_tree.ResidueRetained, match="removal bound reached"):
        tree.destroy()
    monkeypatch.undo()
    verify_tree._RESIDUE_NEXT_SCAN.clear()
    verify_tree._BACKOFF.clear()
    assert verify_tree.sweep(tree.sandbox_root) == []
    assert list((tree.sandbox_root / verify_tree.RESIDUE_DIR).iterdir()) == []


@windows_only
def test_concurrent_removal_during_destroy_reopen_is_residue_not_tamper(source: Path, tmp_path: Path,
                                                                        monkeypatch):
    """LOW-4: another orchestrator process's sweep holding the run directory between close and reopen."""
    tree = _tree(source, tmp_path)
    tree.create()
    held = []
    real = verify_tree._remove_child

    def other_process_sweep(parent, name, label, **kw):
        held.append(verify_tree._open_path(str(tree.base), verify_tree._DELETE | verify_tree._HOLD_ACCESS,
                                           verify_tree._HOLD_SHARE))
        return real(parent, name, label, **kw)

    monkeypatch.setattr(verify_tree, "_remove_child", other_process_sweep)
    try:
        with pytest.raises(verify_tree.ResidueRetained, match="in use"):
            tree.destroy()
    finally:
        monkeypatch.undo()
        for h in held:
            verify_tree._close(h)
    assert verify_tree.sweep(tree.sandbox_root) == [] and not tree.base.exists()


@windows_only
def test_dacl_reset_warning_is_one_summary_line_per_run(source: Path, tmp_path: Path, caplog):
    import logging

    tree = _tree(source, tmp_path)
    tree.create()
    for i in range(5):
        _hostile_dir(tree.base, f"hidden{i}")
    with caplog.at_level(logging.WARNING, logger="orchestrator.security.verify_tree"):
        tree.destroy()
    resets = [r for r in caplog.records if "reset" in r.getMessage() and "DACL" in r.getMessage()]
    assert len(resets) == 1 and "reset 5 DACL" in resets[0].getMessage()
    assert not tree.base.exists()


@windows_only
def test_dacl_reset_refuses_a_multi_link_name(tmp_path: Path):
    """LOW-1: resetting the DACL through a hardlinked name would rewrite the outside file's shared DACL."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    inner = tmp_path / "run"
    inner.mkdir()
    os.link(outside, inner / "hard")
    before = acl.read_security(outside)
    h = verify_tree._open_path(str(inner), verify_tree._HOLD_ACCESS, 7)
    try:
        assert verify_tree._reset_dacl(h, verify_tree._encode("hard"), acl.current_user_sid()) is False
        (inner / "single").write_text("x")
        assert verify_tree._reset_dacl(h, verify_tree._encode("single"), acl.current_user_sid()) is True
    finally:
        verify_tree._close(h)
    assert acl.read_security(outside) == before


@windows_only
def test_scratch_grant_carries_no_write_dac_or_write_owner(tmp_path: Path):
    """MED-B (1): a command cannot re-ACL inherited scratch objects."""
    from orchestrator.security import appcontainer

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    sid = appcontainer.task_sid(scratch)
    appcontainer.grant_directory(scratch, sid, writable=True, private_scratch=True)
    (mine,) = [a for a in acl.read_security(scratch)["aces"] if a["sid"] == sid]
    assert mine["mask"] & (verify_tree._WRITE_DAC | 0x80000) == 0
    assert mine["mask"] & 0x40, "DELETE_CHILD is kept"
