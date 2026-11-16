"""Evidence-bound Codex activation gate: record parsing/tamper, binary/code binding, adapter refusal/acceptance.

No real Codex process is started here (synthetic executables, patched version probe, fake transport),
except in the explicit native opt-in test at the bottom (ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT), which runs
the model-free attestation against the real installed binary with synthetic auth and canaries.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from contract.conftest import FakeSpawner, fixture_lines, make_adapter, request

from orchestrator.providers import codex as codex_module
from orchestrator.providers.base import AgentEventKind, UnsupportedOperation
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ResolvedExecutable
from orchestrator.security import acl
from orchestrator.security import codex_activation as ca

VERSION = "codex-cli 9.9.9"


def _exe(tmp_path: Path, content: bytes = b"synthetic codex binary") -> ResolvedExecutable:
    path = tmp_path / "bin-synthetic" / "codex.exe"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return ResolvedExecutable((str(path),), str(path), "test")


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return data


def _record(exe: ResolvedExecutable, **over) -> dict:
    record = {
        "schema_version": ca.SCHEMA_VERSION, "codex_version": VERSION,
        "executable_sha256": ca.executable_sha256(ca.executable_files(exe)), "executable_path": exe.path,
        "code_identity_sha256": ca.code_identity(),
        "checks": [{"name": name, "result": "PASS"} for name in ca.REQUIRED_CHECKS],
        "attested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    record.update(over)
    return record


async def _version(exe, env):
    return VERSION


def _adapter(tmp_path, exe, spawner=None) -> CodexAdapter:
    adapter = CodexAdapter(spawner=spawner or FakeSpawner([]), resolver=lambda name: exe, environ={"PATH": "C:\\bin"},
                           env_allowlist=["PATH"], data_dir=_data(tmp_path))
    adapter._probe_version = _version
    return adapter


# --------------------------------------------------------------------------- record parsing / tamper

def test_record_round_trip_is_owner_only(tmp_path):
    exe, data = _exe(tmp_path), _data(tmp_path)
    path = ca.write_record(data, _record(exe))
    assert acl.check_private(path) == []
    record, reason = ca.read_record(data)
    assert reason == "" and record == _record(exe) | {"attested_at": record["attested_at"]}
    assert not [p for p in data.iterdir() if p.name.endswith(".tmp")]


def test_missing_record_is_not_active(tmp_path):
    record, reason = ca.read_record(_data(tmp_path))
    assert record is None and reason.startswith("missing")


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(extra=1),
    lambda r: r.pop("attested_at"),
    lambda r: r.update(schema_version=2),
    lambda r: r.update(schema_version=True),
    lambda r: r.update(executable_sha256="0" * 63),
    lambda r: r.update(executable_sha256="A" * 64),
    lambda r: r.update(code_identity_sha256=None),
    lambda r: r.update(codex_version=""),
    lambda r: r.update(codex_version="codex\n0.1.0"),
    lambda r: r["checks"].pop(),
    lambda r: r["checks"].reverse(),
    lambda r: r["checks"][3].update(result="FAIL"),
    lambda r: r["checks"][0].update(note="x"),
    lambda r: r.update(attested_at="2026-11-15 10:00:00"),
    lambda r: r.update(attested_at=(datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
])
def test_strict_schema_rejects_every_deviation(tmp_path, mutate):
    record = _record(_exe(tmp_path))
    mutate(record)
    with pytest.raises(ca.ActivationRecordInvalid):
        ca.parse_record(json.dumps(record).encode())
    with pytest.raises(ca.ActivationRecordInvalid):
        ca.write_record(_data(tmp_path), record)
    assert not ca.record_path(_data(tmp_path)).exists()


@pytest.mark.parametrize("raw", [b"", b"\xff\xfe", b"{", b"[]", b"null", b'{"a":NaN}', b" " * (ca.MAX_RECORD_BYTES + 1)])
def test_unparseable_records_are_rejected(raw):
    with pytest.raises(ca.ActivationRecordInvalid):
        ca.parse_record(raw)


def test_duplicate_keys_are_rejected(tmp_path):
    text = json.dumps(_record(_exe(tmp_path)))
    with pytest.raises(ca.ActivationRecordInvalid):
        ca.parse_record(text.replace('{"schema_version": 1', '{"schema_version": 1, "schema_version": 1', 1).encode())


def test_tampered_record_on_disk_is_not_active(tmp_path):
    exe, data = _exe(tmp_path), _data(tmp_path)
    path = ca.write_record(data, _record(exe))
    path.write_bytes(path.read_bytes().replace(b'"PASS"', b'"FAIL"', 1))  # ACL unchanged, content tampered
    record, reason = ca.read_record(data)
    assert record is None and reason.startswith("invalid")


def test_oversized_record_is_not_active(tmp_path):
    exe, data = _exe(tmp_path), _data(tmp_path)
    path = ca.write_record(data, _record(exe))
    path.write_bytes(b" " * (ca.MAX_RECORD_BYTES + 10))
    record, reason = ca.read_record(data)
    assert record is None and "size" in reason


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_record_without_owner_only_acl_is_not_active(tmp_path):
    exe, data = _exe(tmp_path), _data(tmp_path)
    ca.record_path(data).write_bytes(json.dumps(_record(exe)).encode())  # inherited (non-private) ACL
    assert acl.check_private(ca.record_path(data))
    record, reason = ca.read_record(data)
    assert record is None and "owner-only" in reason


def test_hardlinked_record_is_not_active(tmp_path):
    exe, data = _exe(tmp_path), _data(tmp_path)
    path = ca.write_record(data, _record(exe))
    os.link(path, tmp_path / "alias.json")
    record, reason = ca.read_record(data)
    assert record is None and reason.startswith("invalid")


def test_record_that_is_a_directory_is_not_active(tmp_path):
    data = _data(tmp_path)
    ca.record_path(data).mkdir()
    record, reason = ca.read_record(data)
    assert record is None and reason.startswith("invalid")


# --------------------------------------------------------------------------- identities

def test_npm_layout_hashes_interpreter_and_whole_package(tmp_path):
    node = tmp_path / "node" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(b"node")
    root = tmp_path / "pkg"
    (root / "bin").mkdir(parents=True)
    (root / "package.json").write_text("{}")
    entry = root / "bin" / "codex.js"
    entry.write_text("// shim")
    vendor = root / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "bin"
    vendor.mkdir(parents=True)
    (vendor / "codex.exe").write_bytes(b"native-1")
    exe = ResolvedExecutable((str(node), str(entry)), str(entry), "test")
    files = ca.executable_files(exe)
    labels = [label for label, _ in files]
    assert "argv0:node.exe" in labels and "package:node_modules/@openai/codex-win32-x64/vendor/bin/codex.exe" in labels
    before = ca.executable_sha256(files)
    (vendor / "codex.exe").write_bytes(b"native-2")
    assert ca.executable_sha256(ca.executable_files(exe)) != before
    entry2 = tmp_path / "loose.js"
    entry2.write_text("x")
    with pytest.raises(ca.ActivationUnavailable):
        ca.executable_files(ResolvedExecutable((str(node), str(entry2)), str(entry2), "test"))


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_package_with_a_link_is_unsupported(tmp_path):
    import subprocess

    root = tmp_path / "pkg"
    (root / "bin").mkdir(parents=True)
    (root / "package.json").write_text("{}")
    (root / "bin" / "codex.js").write_text("x")
    (tmp_path / "elsewhere").mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "linked"), str(tmp_path / "elsewhere")],
                       check=False, capture_output=True)
    if r.returncode != 0:
        pytest.skip("could not create a junction")
    node = tmp_path / "node.exe"
    node.write_bytes(b"n")
    exe = ResolvedExecutable((str(node), str(root / "bin" / "codex.js")), "x", "test")
    with pytest.raises(ca.ActivationUnavailable):
        ca.executable_files(exe)


def test_native_vendor_tree_is_included(tmp_path):
    triple = tmp_path / "x86_64-pc-windows-msvc"
    (triple / "bin").mkdir(parents=True)
    (triple / "codex-resources").mkdir()
    (triple / "bin" / "codex.exe").write_bytes(b"main")
    helper = triple / "codex-resources" / "codex-windows-sandbox-setup.exe"
    helper.write_bytes(b"helper-1")
    exe = ResolvedExecutable((str(triple / "bin" / "codex.exe"),), "x", "test")
    before = ca.executable_sha256(ca.executable_files(exe))
    helper.write_bytes(b"helper-2")
    assert ca.executable_sha256(ca.executable_files(exe)) != before


async def test_identity_cache_reprobes_only_after_a_change(tmp_path):
    exe = _exe(tmp_path)
    calls = []

    async def probe(e, env):
        calls.append(1)
        return VERSION

    cache = ca.BinaryIdentityCache()
    first = await cache.current(exe, {}, version_probe=probe)
    assert (await cache.current(exe, {}, version_probe=probe)) is first and len(calls) == 1
    Path(exe.path).write_bytes(b"replaced binary bytes")
    second = await cache.current(exe, {}, version_probe=probe)
    assert second.executable_sha256 != first.executable_sha256 and len(calls) == 2


async def test_version_probe_failure_is_unavailable(tmp_path):
    async def probe(e, env):
        return None

    with pytest.raises(ca.ActivationUnavailable):
        await ca.BinaryIdentityCache().current(_exe(tmp_path), {}, version_probe=probe)


def test_parse_version():
    assert ca.parse_version("\ncodex-cli 0.169.0\n") == "codex-cli 0.169.0"
    assert ca.parse_version("no version here") is None
    assert ca.parse_version("") is None


def test_code_identity_covers_every_isolation_source():
    entries = ca._snapshot_sources()  # sources unchanged since import in this test process
    assert ca._compute_code_identity(entries) == ca.code_identity()
    labels = {label for label, _ in ca.IDENTITY_MODULES}
    assert {"security/codex_runtime.py", "security/codex_auth.py", "security/codex_activation.py",
            "providers/codex.py", "providers/process.py", "gitops/git.py", "security/paths.py",
            "security/acl.py", "security/worker_job.py"} <= labels
    for index in range(len(entries)):
        changed = list(entries)
        label, size, _ = entries[index]
        changed[index] = (label, size, "0" * 64)
        assert ca._compute_code_identity(tuple(changed)) != ca.code_identity()


def test_code_identity_is_an_import_time_snapshot(monkeypatch):
    """A running process never re-reads its sources: editing them on disk cannot make old in-memory code
    match a record attested for new code."""
    before = ca.code_identity()

    def refuse(*_a, **_k):
        raise AssertionError("code identity must not be recomputed from disk")

    monkeypatch.setattr(ca, "_snapshot_sources", refuse)
    monkeypatch.setattr(ca.Path, "read_bytes", refuse)
    assert ca.code_identity() == before


def test_denial_requires_a_completed_process():
    assert ca._denied(1) is True and ca._denied(0) is False
    for code in (None, "1", True):
        with pytest.raises(ca._Probe):
            ca._denied(code)


# --------------------------------------------------------------------------- adapter gate

async def _refusal(adapter) -> str:
    with pytest.raises(UnsupportedOperation) as info:
        await adapter._require_activated_runtime()
    return str(info.value)


async def test_adapter_refuses_without_record(tmp_path):
    exe = _exe(tmp_path)
    spawner = FakeSpawner([])
    adapter = _adapter(tmp_path, exe, spawner)
    message = await _refusal(adapter)
    assert "owned runtime activation missing" in message
    events = [e async for e in adapter.execute(request(run_id="r-none", cwd=str(tmp_path / "work")))]
    assert not spawner.calls
    assert events[-1].kind == AgentEventKind.RESULT and events[-1].data["error_category"] == "invalid_request"
    assert "owned runtime activation missing" in events[-1].data["error_message"]


@pytest.mark.parametrize("field,value,expected", [
    ("codex_version", "codex-cli 0.167.1", "stale: attested for"),
    ("executable_sha256", "0" * 64, "executable hash differs"),
    ("code_identity_sha256", "f" * 64, "isolation source changed"),
])
async def test_adapter_refuses_stale_record(tmp_path, field, value, expected):
    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    ca.write_record(adapter.data_dir, _record(exe, **{field: value}))
    message = await _refusal(adapter)
    assert "owned runtime activation" in message and expected in message


async def test_adapter_refuses_after_binary_replacement(tmp_path):
    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    ca.write_record(adapter.data_dir, _record(exe))
    await adapter._require_activated_runtime()
    Path(exe.path).write_bytes(b"a different binary")
    assert "executable hash differs" in await _refusal(adapter)


async def test_adapter_refuses_when_record_is_removed(tmp_path):
    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    ca.write_record(adapter.data_dir, _record(exe)).unlink()
    assert "missing" in await _refusal(adapter)


async def test_adapter_refuses_unresolvable_or_unhashable_executable(tmp_path):
    from orchestrator.providers.process import ExecutableResolutionError

    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    ca.write_record(adapter.data_dir, _record(exe))

    def missing(name):
        raise ExecutableResolutionError("not found")

    adapter._resolver = missing
    assert "unavailable" in await _refusal(adapter)
    ghost = ResolvedExecutable((str(tmp_path / "ghost.exe"),), "ghost", "test")
    adapter._resolver = lambda name: ghost
    assert "unavailable" in await _refusal(adapter)


async def test_environment_cannot_bypass_the_gate(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    for name in ("ORCHESTRATOR_CODEX_ACTIVATED", "ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT", "CODEX_ACTIVATION"):
        monkeypatch.setenv(name, "1")
    adapter._environ = {"PATH": "C:\\bin", "ORCHESTRATOR_CODEX_ACTIVATED": "1"}
    assert "missing" in await _refusal(adapter)


async def test_matching_record_activates_fake_transport_execution(tmp_path):
    """A matching synthetic record lets the real gate pass; the transport is fake (no real codex exec)."""
    exe = _exe(tmp_path)
    spawner = FakeSpawner(fixture_lines("codex_success.jsonl"))
    adapter = make_adapter("codex", spawner)
    del adapter._require_activated_runtime  # restore the production gate on this instance
    adapter._resolver = lambda name: exe
    adapter._data_dir = _data(tmp_path)
    adapter._probe_version = _version
    ca.write_record(adapter.data_dir, _record(exe))
    events = [e async for e in adapter.execute(request(run_id="r-active", cwd=str(tmp_path / "work")))]
    assert len(spawner.calls) == 1
    assert events[-1].kind == AgentEventKind.RESULT and events[-1].data["success"] is True


async def test_detect_reports_activation_without_mcp_inventory(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    adapter = _adapter(tmp_path, exe)
    calls = []

    async def fake_capture(argv, **kw):
        calls.append(list(argv))
        if "--version" in argv:
            return 0, "codex-cli 9.9.9\n", ""
        return 0, "Logged in using ChatGPT\n", ""

    monkeypatch.setattr(codex_module, "run_capture", fake_capture)
    adapter.user_mcp_servers = ["stale-user-server"]
    status = await adapter.detect()
    assert "owned runtime activation missing" in status.error and status.capabilities.sandbox_modes == []
    ca.write_record(adapter.data_dir, _record(exe))
    status = await adapter.detect()
    assert status.error is None and status.capabilities.sandbox_modes == ["read_only", "workspace_write"]
    assert adapter.user_mcp_servers == []
    assert not any("mcp" in argv for argv in calls)



# --------------------------------------------------------------------------- native opt-in

@pytest.mark.skipif(os.name != "nt", reason="native Windows owned runtime")
async def test_native_attest_end_to_end_with_real_binary():
    """Model-free: runs the full attestation against the installed Codex with synthetic auth/canaries."""
    value = os.environ.get("ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT")
    if not value:
        pytest.skip("explicit private non-TEMP native data fixture required")
    from orchestrator.security import codex_runtime as rt
    from orchestrator.security import paths

    parent = Path(value)
    assert parent.is_absolute() and not acl.check_private(parent)
    data = parent / ("at-" + uuid.uuid4().hex[:8])  # short: see MAX_DATA_DIR_CHARS
    with paths.directory_guard(parent):
        rt._WindowsIO().directory(data, exclusive=True)
    result = await ca.attest(data)
    print(json.dumps(result.as_dict(), indent=2))
    assert result.error is None, result.error
    assert [c.result for c in result.checks] == ["PASS"] * len(ca.REQUIRED_CHECKS), result.as_dict()
    assert result.recorded and result.cleanup == "removed"
    record, reason = ca.read_record(data)
    assert record is not None, reason
    adapter = CodexAdapter(data_dir=data)
    await adapter._require_activated_runtime()  # real binary hash + real `codex --version`
    ca.record_path(data).unlink()
    with pytest.raises(UnsupportedOperation, match="owned runtime activation missing"):
        await adapter._require_activated_runtime()


# --------------------------------------------------------------------------- CLI / doctor

def test_cli_codex_attest_exit_codes(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from orchestrator import config
    from orchestrator.cli import main as main_mod

    monkeypatch.setenv(config.ENV_DATA_DIR, str(_data(tmp_path)))
    runner = CliRunner()
    r = runner.invoke(main_mod.app, ["codex-attest"])  # the suite disables real providers
    assert r.exit_code == 1 and "nothing was run" in (r.stdout + r.stderr)

    monkeypatch.setattr(config, "real_providers_disabled", lambda: False)
    seen = []

    async def failing(data_dir):
        seen.append(Path(data_dir))
        return ca.AttestationResult(recorded=False, checks=[ca.CheckResult("sandbox_positive_control", "FAIL", "x")],
                                    cleanup="removed")

    monkeypatch.setattr(ca, "attest", failing)
    r = runner.invoke(main_mod.app, ["codex-attest"])
    assert r.exit_code == 1 and "NOT recorded" in r.stdout and "sandbox_positive_control" in r.stdout
    assert seen == [config.get_paths().data_dir]

    async def passing(data_dir):
        return ca.AttestationResult(recorded=True, record_path=str(ca.record_path(data_dir)), cleanup="removed",
                                    checks=[ca.CheckResult(n, "PASS") for n in ca.REQUIRED_CHECKS])

    monkeypatch.setattr(ca, "attest", passing)
    r = runner.invoke(main_mod.app, ["codex-attest", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["recorded"] is True


def test_doctor_reports_activation_state(tmp_path, monkeypatch):
    from orchestrator import config
    from orchestrator.cli import doctor as doctor_mod
    from orchestrator.providers.base import ProviderStatus

    data = _data(tmp_path)
    monkeypatch.setenv(config.ENV_DATA_DIR, str(data))
    check = doctor_mod.codex_activation_check(None)
    assert check["status"] == "warn" and "missing" in check["detail"] and "codex-attest" in check["fix"]
    exe = _exe(tmp_path)
    ca.write_record(data, _record(exe))
    assert doctor_mod.codex_activation_check(None)["status"] == "ok"
    stale = ProviderStatus(provider="codex", installed=True, error=ca.refusal_message("stale: executable hash differs"))
    assert doctor_mod.codex_activation_check({"codex": stale})["status"] == "warn"
    fine = ProviderStatus(provider="codex", installed=True)
    assert doctor_mod.codex_activation_check({"codex": fine})["status"] == "ok"
    ca.write_record(data, _record(exe, code_identity_sha256="e" * 64))
    assert "isolation source changed" in doctor_mod.codex_activation_check(None)["detail"]
