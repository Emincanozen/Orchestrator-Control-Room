"""Synthetic ACL readback tests: no Windows ACL, process or profile changes."""
import ctypes
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from orchestrator.security import acl, appcontainer, paths, sandbox

SID = "S-1-15-2-123"
MODIFY = 0x1301BF


def ace(mask=MODIFY, flags=3, kind=0, sid=SID):
    return {"type": kind, "sid": sid, "mask": mask, "flags": flags,
            "inherited": bool(flags & 16)}


@pytest.fixture(params=[False, True], ids=["opened-root", "pinned-root"])
def native(request, monkeypatch, tmp_path):
    state = SimpleNamespace(aces=[], closed=[])

    class Info(ctypes.Structure):
        _fields_ = [("dwFileAttributes", ctypes.c_uint32)]

    def information(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(Info)).contents.dwFileAttributes = 0
        return True

    monkeypatch.setattr(paths, "directory_guard", lambda path: nullcontext())
    monkeypatch.setattr(acl, "read_security", lambda path: {"aces": state.aces})
    monkeypatch.setattr(sandbox, "_handle_dacl_sddl", lambda handle: f"D:(A;OICI;FA;;;{SID})", raising=False)
    monkeypatch.setattr(sandbox, "INVALID_HANDLE_VALUE", -1, raising=False)
    monkeypatch.setattr(sandbox, "_BY_HANDLE_FILE_INFORMATION", Info, raising=False)
    monkeypatch.setattr(sandbox, "_k32", SimpleNamespace(
        CreateFileW=lambda *args: -1 if request.param else 42,
        GetFileInformationByHandle=information,
        CloseHandle=lambda handle: state.closed.append(handle) or True,
    ), raising=False)
    state.path = tmp_path / "synthetic-root"
    return state


@pytest.mark.parametrize("flags", [3, 19])
def test_single_effective_exact_grant_is_accepted(native, flags):
    native.aces = [ace(flags=flags), ace(mask=0x1F01FF, sid="S-1-5-18")]
    appcontainer.grant_directory(native.path, SID, writable=True)


@pytest.mark.parametrize("entries", [
    [ace(), ace(mask=0x1F01FF)],  # A valid narrow ACE must not hide a broader one.
    [ace(), ace()],
    [ace(), ace(kind=1)],
    [ace(mask=0x1F01FF)],
    [],
])
def test_inexact_same_sid_acl_fails_without_repair(native, entries):
    native.aces = entries
    with pytest.raises(appcontainer.AppContainerUnavailable):
        appcontainer.grant_directory(native.path, SID, writable=True)
    assert native.aces == entries


@pytest.mark.parametrize("private_scratch,mask", [(False, MODIFY), (True, 0x1301FF)])  # scratch: no WRITE_DAC/OWNER
def test_exact_grant_matches_requested_directory_scope(native, private_scratch, mask):
    native.aces = [ace(mask=mask)]
    appcontainer.grant_directory(native.path, SID, writable=True, private_scratch=private_scratch)
