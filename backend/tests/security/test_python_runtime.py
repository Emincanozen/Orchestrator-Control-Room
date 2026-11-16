"""Private runtime staging never modifies the original Python installation."""

import hashlib
import json
import os

import pytest

from orchestrator.security import acl
from orchestrator.security.python_runtime import RuntimeLease, RuntimeUnavailable, stage_python, validate_runtime


def sources(tmp_path):
    base = tmp_path / "original-python"
    base.mkdir()
    (base / "python.exe").write_bytes(b"interpreter")
    (base / "Lib").mkdir()
    (base / "Lib" / "os.py").write_bytes(b"stdlib")
    site = tmp_path / "venv-packages"
    site.mkdir()
    (site / "pytest.py").write_bytes(b"pytest")
    (site / "editable.pth").write_text("C:/private/source")
    return base, site


def test_stage_copies_runtime_without_source_hooks(tmp_path):
    base, site = sources(tmp_path)
    exe = stage_python(tmp_path / "copy", base=base, site_packages=site)
    assert exe.read_bytes() == (base / "python.exe").read_bytes() == b"interpreter"
    assert (exe.parent / "Lib/site-packages/pytest.py").read_bytes() == b"pytest"
    assert not (exe.parent / "Lib/site-packages/editable.pth").exists()
    assert (site / "editable.pth").read_text() == "C:/private/source"
    manifest = json.loads((exe.parent / "runtime-manifest.json").read_text())
    assert manifest["files"]["python.exe"] == hashlib.sha256(b"interpreter").hexdigest()
    assert acl.check_private(exe.parent) == []


def test_existing_runtime_is_never_overwritten(tmp_path):
    base, site = sources(tmp_path)
    dest = tmp_path / "copy"
    dest.mkdir()
    (dest / "keep").write_text("untouched")
    with pytest.raises(RuntimeUnavailable, match="already exists"):
        stage_python(dest, base=base, site_packages=site)
    assert (dest / "keep").read_text() == "untouched"


def test_source_overlap_is_rejected(tmp_path):
    base, site = sources(tmp_path)
    with pytest.raises(RuntimeUnavailable, match="overlaps"):
        stage_python(base / "copy", base=base, site_packages=site)


def test_acl_failure_never_returns_a_runtime(tmp_path, monkeypatch):
    base, site = sources(tmp_path)
    monkeypatch.setattr(acl, "harden_private_dir", lambda path: False)
    with pytest.raises(RuntimeUnavailable, match="private ACL"):
        stage_python(tmp_path / "copy", base=base, site_packages=site)
    assert not (tmp_path / "copy" / "python.exe").exists()


@pytest.mark.parametrize("change", ["content", "added-hook", "added-bytecode", "missing"])
def test_reuse_refuses_changed_runtime(tmp_path, change):
    base, site = sources(tmp_path)
    exe = stage_python(tmp_path / "copy", base=base, site_packages=site)
    assert validate_runtime(exe.parent) == exe
    if change == "content":
        exe.write_bytes(b"changed")
    elif change == "missing":
        exe.unlink()
    elif change == "added-hook":
        (exe.parent / "injected.pth").write_text("import secret")
    else:
        cache = exe.parent / "__pycache__"
        cache.mkdir()
        (cache / "injected.pyc").write_bytes(b"code")
    with pytest.raises(RuntimeUnavailable, match="changed"):
        validate_runtime(exe.parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and ACL checks")
def test_unsafe_existing_managed_parent_is_refused_without_acl_changes(tmp_path):
    parent = tmp_path / "data" / "verification-runtimes"
    parent.mkdir(parents=True)
    (parent / "existing-marker").write_text("untouched")
    before = acl.read_security(parent)
    assert acl.check_private(parent)
    with pytest.raises(RuntimeUnavailable, match="not private and trusted"):
        RuntimeLease(parent).acquire()
    assert acl.read_security(parent) == before
    assert (parent / "existing-marker").read_text() == "untouched"
    assert not (parent / "python-v2").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and ACL checks")
def test_runtime_lease_prevents_ancestor_runtime_and_module_replacement(tmp_path):
    base, site = sources(tmp_path)
    parent = tmp_path / "data" / "verification-runtimes"
    lease = RuntimeLease(parent)
    try:
        exe = lease.acquire(base=base, site_packages=site)
        assert exe.parent == parent / "python-v2"
        assert acl.check_private(parent) == []
        for target in (parent.parent, parent, exe.parent, exe.parent / "Lib"):
            with pytest.raises(OSError) as error:
                target.rename(target.with_name(target.name + "-swapped"))
            assert error.value.winerror == 32
        for target in (exe, exe.parent / "Lib" / "os.py"):
            with pytest.raises(OSError) as error:
                target.write_bytes(b"replacement")
            assert error.value.errno == 13 or error.value.winerror == 32
    finally:
        lease.close()
    parent.rename(parent.with_name("released-runtime-parent"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and ACL checks")
def test_interrupted_staging_is_removed_and_restaged(tmp_path):
    """Staging writes runtime-manifest.json last. A copy left without it (process stopped during the first,
    minute-long staging) must not block verification forever: it is removed without following links and
    staged again. A complete copy is never touched."""
    base, site = sources(tmp_path)
    parent = tmp_path / "data" / "verification-runtimes"
    first = RuntimeLease(parent)
    try:
        first.acquire(base=base, site_packages=site)
    finally:
        first.close()
    copy = parent / "python-v2"
    (copy / "runtime-manifest.json").unlink()  # simulate staging interrupted before the manifest
    (copy / "leftover-partial.bin").write_bytes(b"partial")
    lease = RuntimeLease(parent)
    try:
        exe = lease.acquire(base=base, site_packages=site)
        assert exe == copy / "python.exe"
        assert (copy / "runtime-manifest.json").is_file()
        assert not (copy / "leftover-partial.bin").exists()
    finally:
        lease.close()
    # A complete copy is reused as is (never restaged): its marker survives a new acquisition.
    marker_mtime = (copy / "runtime-manifest.json").stat().st_mtime_ns
    again = RuntimeLease(parent)
    try:
        again.acquire(base=base, site_packages=site)
    finally:
        again.close()
    assert (copy / "runtime-manifest.json").stat().st_mtime_ns == marker_mtime
