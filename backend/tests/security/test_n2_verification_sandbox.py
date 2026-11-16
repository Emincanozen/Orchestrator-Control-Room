"""Finding N2: verification commands run agent-written code; they run in a Low-integrity sandbox.

Real Windows mechanisms throughout (tokens, mandatory labels, jobs, git, sqlite). The only seams are failure
injections for the fail-closed tests. The hostile code is a Python script run through the production
verification path (`Verifier.run_checks` -> `run_command` -> `sandbox.Session`), playing a malicious conftest.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
import pytest

import orchestrator
from orchestrator.config import CONFIG_DIR, child_env, effective_policies, get_paths
from orchestrator.gitops import git, worktrees
from orchestrator.scheduler import verification
from orchestrator.scheduler.verification import Verifier, run_command
from orchestrator.security import sandbox, worker_job
from orchestrator.security.auth import load_or_create_token
from orchestrator.services.context import AppContext

IS_WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="the verification sandbox is Windows-only")
PY = sys.executable


# ----------------------------------------------------------------------------------------------- fixtures


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.paths = get_paths(root / "data", root / "worktrees")
        self.paths.ensure(harden=True)  # real owner-only ACL + the no-read-up integrity label
        self.ctx = AppContext.open(self.paths)  # real database (WAL mode)
        self.token = load_or_create_token(self.paths.token_path)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("base\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "init")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        # Production path: the worktree gets no sandbox grant or label; verification runs in a disposable copy.
        self.wt = Path(worktrees.create(self.repo, self.paths.worktrees_dir / "task1", "task1", self.base).path)
        self.config = root / "config-copy"
        shutil.copytree(CONFIG_DIR, self.config)

    def verifier(self) -> Verifier:
        return Verifier(self.ctx)

    def run(self, specs: list, cwd: Path | None = None) -> tuple[str, list]:
        return asyncio.run(self.verifier().run_checks(specs, str(cwd or self.wt), project_id="proj1"))

    def close(self) -> None:
        self.ctx.close()


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


HOSTILE = r'''
import ctypes, json, os, sqlite3, subprocess, sys
from ctypes import wintypes as wt
spec = json.load(open(sys.argv[1]))
res = {}
def t(name, fn):
    try:
        out = fn()
        res[name] = "ALLOWED" + ("" if out is None else ":" + str(out)[:80])
    except BaseException as e:  # noqa
        res[name] = "blocked:" + type(e).__name__ + ":" + str(e)[:60]
def append(p): open(p, "ab").write(b"pwn")
def write_new(p): open(p, "wb").write(b"pwn")
def read(p): open(p, "rb").read(4); return "read"
def sql_update(p):
    c = sqlite3.connect(p, timeout=1)
    c.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES ('approvals', '{\"auto_approve\": [\"all\"]}', 'x')")
    c.commit(); c.close()
def sql_read(p):
    c = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=1)
    return c.execute("select count(*) from settings").fetchone()
def run(argv):
    r = subprocess.run(argv, capture_output=True, timeout=60, creationflags=0x08000000)
    if r.returncode != 0:
        raise PermissionError(f"exit={r.returncode}")
for name, op, path in spec["ops"]:
    t(name, {"append": lambda: append(path), "write_new": lambda: write_new(path), "read": lambda: read(path),
             "delete": lambda: os.remove(path), "rename": lambda: os.rename(path, path + ".moved"),
             "sql_update": lambda: sql_update(path), "sql_read": lambda: sql_read(path),
             "listdir": lambda: os.listdir(path),
             "hardlink_write": lambda: (os.link(path[0], path[1]), append(path[1])),
             "symlink": lambda: os.symlink(path[0], path[1]),
             "junction_write": lambda: (run(["cmd.exe", "/c", "mklink", "/J", path[0], path[1]]), write_new(os.path.join(path[0], "pwn.txt"))),
             "run": lambda: run(path)}[op])
# Raise the own token back to Medium / steal a Medium token.
adv = ctypes.WinDLL("advapi32", use_last_error=True); k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetCurrentProcess.restype = wt.HANDLE; k.OpenProcess.restype = wt.HANDLE
def raise_own():
    h = wt.HANDLE()
    if not adv.OpenProcessToken(wt.HANDLE(k.GetCurrentProcess()), 0x0080 | 0x0008, ctypes.byref(h)): raise OSError("open")
    sid = ctypes.c_void_p(); adv.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid))
    class SA(ctypes.Structure): _fields_ = [("Sid", ctypes.c_void_p), ("Attr", wt.DWORD)]
    sa = SA(sid.value, 0x20)
    if not adv.SetTokenInformation(h, 25, ctypes.byref(sa), ctypes.sizeof(sa) + adv.GetLengthSid(sid)):
        raise PermissionError(f"code={ctypes.get_last_error()}")
def steal(pid):
    h = k.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
    if not h: raise PermissionError(f"code={ctypes.get_last_error()}")
    tok = wt.HANDLE()
    if not adv.OpenProcessToken(wt.HANDLE(h), 0x0002, ctypes.byref(tok)): raise PermissionError(f"code={ctypes.get_last_error()}")
def relabel(path):  # SetNamedSecurityInfoW(LABEL) to Low on a Medium object
    psd = ctypes.c_void_p(); adv.ConvertStringSecurityDescriptorToSecurityDescriptorW("S:(ML;;NW;;;LW)", 1, ctypes.byref(psd), None)
    pres, dflt, sacl = wt.BOOL(), wt.BOOL(), ctypes.c_void_p()
    adv.GetSecurityDescriptorSacl(psd, ctypes.byref(pres), ctypes.byref(sacl), ctypes.byref(dflt))
    err = adv.SetNamedSecurityInfoW(path, 1, 0x10, None, None, None, sacl)
    if err: raise PermissionError(f"code={err}")
def breakaway():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=0x01000000 | 0x08000000)
    return p.pid
t("raise_own_token", raise_own)
t("steal_parent_token", lambda: steal(spec["parent_pid"]))
for i, p in enumerate(spec.get("relabel", [])):
    t(f"relabel_{i}", lambda: relabel(p))
t("breakaway", breakaway)
print("HOSTILE-RESULTS=" + json.dumps(res))
print("HOSTILE-DONE")
'''


def _attack(env: Env, ops: list, **extra) -> dict:
    script = env.wt / "hostile_conftest.py"
    script.write_text(HOSTILE)
    spec = env.wt / "hostile-spec.json"
    spec.write_text(json.dumps({"ops": ops, "parent_pid": os.getpid(), **extra}))
    # The command runs in a disposable copy of the worktree: results come back on stdout, never as files.
    status, results = env.run([[PY, str(script), str(spec)]])
    assert results[0].error is None, results[0]
    assert "HOSTILE-DONE" in results[0].output, results[0].output
    res = json.loads(results[0].output.split("HOSTILE-RESULTS=", 1)[1].splitlines()[0])
    print(json.dumps(res, indent=1))  # evidence in -s runs
    return res


# ----------------------------------------------------------------------------------------------- tests


@windows_only
def test_verification_child_runs_at_low_integrity_inside_the_worker_job(env: Env):
    code = ("import ctypes,sys,time;from ctypes import wintypes as w;a=ctypes.WinDLL('advapi32');k=ctypes.WinDLL('kernel32');"
            "k.GetCurrentProcess.restype=w.HANDLE;h=w.HANDLE();a.OpenProcessToken(w.HANDLE(k.GetCurrentProcess()),8,ctypes.byref(h));"
            "b=ctypes.create_string_buffer(64);n=w.DWORD();a.GetTokenInformation(h,25,b,64,ctypes.byref(n));"
            "s=ctypes.c_void_p.from_buffer(b).value;a.GetSidSubAuthorityCount.restype=ctypes.POINTER(ctypes.c_ubyte);"
            "a.GetSidSubAuthority.restype=ctypes.POINTER(w.DWORD);c=a.GetSidSubAuthorityCount(ctypes.c_void_p(s)).contents.value;"
            "print('IL=0x%x'%a.GetSidSubAuthority(ctypes.c_void_p(s),c-1).contents.value)")
    status, results = env.run([[PY, "-c", code]])
    assert status == "passed", results
    assert "IL=0x1000" in results[0].output
    # From outside, through the production session API: integrity and job membership of the live process.
    session = sandbox.Session(str(env.wt), env.paths.sandbox_root("proj1"), writable_cwd=True).prepare()
    fd, log_path = tempfile.mkstemp(dir=env.paths.tmp_dir)
    try:
        proc, job = verification.sandbox_start(session, [PY, "-c", "import time; time.sleep(20)"], dict(os.environ), fd)
        try:
            assert sandbox.process_integrity_rid(proc.pid) == sandbox.LOW_RID
            assert worker_job.is_in_job(proc.pid) is True
        finally:
            job.close()  # ends the process
            proc.wait(10)
            proc.close()
    finally:
        os.close(fd)
        session.finish()
        os.unlink(log_path)


@windows_only
def test_hostile_verification_code_cannot_touch_orchestrator_state(env: Env):
    db = env.paths.db_path
    token = env.paths.token_path
    policies = env.config / "policies.yaml"
    marker_src = Path(orchestrator.__file__).parent / f"_n2_marker_{uuid.uuid4().hex}.py"
    marker_profile = Path(os.environ["USERPROFILE"]) / f"_n2_marker_{uuid.uuid4().hex}.txt"
    marker_temp = Path(tempfile.gettempdir()) / f"_n2_marker_{uuid.uuid4().hex}.txt"
    sibling = env.paths.worktrees_dir / "sibling"
    sibling.mkdir()
    git_config = env.repo / ".git" / "config"
    hook = env.repo / ".git" / "hooks" / "pre-commit"
    before = {p: _sha(p) for p in (db, token, policies, git_config)}
    wal = Path(str(db) + "-wal")
    ops = [
        ["db_append", "append", str(db)], ["db_delete", "delete", str(db)], ["db_rename", "rename", str(db)],
        ["db_read", "read", str(db)], ["db_sql_update", "sql_update", str(db)], ["db_sql_read", "sql_read", str(db)],
        ["wal_read", "read", str(wal)], ["data_listdir", "listdir", str(env.paths.data_dir)],
        ["data_new_file", "write_new", str(env.paths.data_dir / "planted.txt")],
        ["token_read", "read", str(token)], ["token_append", "append", str(token)],
        ["policies_write", "append", str(policies)], ["source_marker", "write_new", str(marker_src)],
        ["profile_write", "write_new", str(marker_profile)], ["temp_write", "write_new", str(marker_temp)],
        ["git_config", "append", str(git_config)], ["git_hook", "write_new", str(hook)],
        ["sibling_write", "write_new", str(sibling / "pwn.txt")],
        ["real_worktree_write", "write_new", str(env.wt / "pwn.txt")],  # the real worktree is not writable
        ["real_worktree_append", "append", str(env.wt / "README.md")],
        ["real_worktree_read", "read", str(env.wt / "README.md")],  # nor readable: only the copy is granted
        ["worktree_write", "write_new", "legit.txt"],  # control: the command's own copy is writable
    ]
    wt_before = {p.name: _sha(p) for p in env.wt.iterdir() if p.is_file()}
    try:
        res = _attack(env, ops)
    finally:
        for m in (marker_src, marker_profile, marker_temp):
            if m.exists():
                m.unlink()
    assert res.pop("worktree_write") == "ALLOWED"
    allowed = {k: v for k, v in res.items() if k in dict((o[0], 1) for o in ops) and v.startswith("ALLOWED")}
    assert not allowed, allowed
    for p, h in before.items():
        assert p.exists() and _sha(p) == h, p
    for m in (marker_src, marker_profile, marker_temp, hook, sibling / "pwn.txt", env.paths.data_dir / "planted.txt",
              env.wt / "pwn.txt", env.wt / "legit.txt"):
        assert not m.exists(), m
    assert all(_sha(env.wt / name) == digest for name, digest in wt_before.items())
    # The Medium orchestrator still reads its own state.
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM settings").fetchone() is not None
    finally:
        con.close()
    assert token.read_text(encoding="utf-8").strip() == env.token


@windows_only
@pytest.mark.skip(reason="restricted live-alias probe: not re-run. Copy-in (security/verify_tree.py) removed its premise - the command cannot write the real worktree; see tests/security/test_verify_tree.py")
def test_escape_attempts_fail(env: Env):
    sibling = env.paths.worktrees_dir / "sibling"
    sibling.mkdir()
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    task_name = f"OrchN2Probe{uuid.uuid4().hex[:10]}"
    ops = [
        ["icacls_data_low", "run", [icacls, str(env.paths.data_dir), "/setintegritylevel", "(OI)(CI)low"]],
        ["icacls_db_low", "run", [icacls, str(env.paths.db_path), "/setintegritylevel", "low"]],
        ["icacls_sibling_low", "run", [icacls, str(sibling), "/setintegritylevel", "(OI)(CI)low"]],
        ["icacls_worktree_medium", "run", [icacls, str(env.wt), "/setintegritylevel", "medium"]],
        ["icacls_data_reset", "run", [icacls, str(env.paths.data_dir), "/reset"]],
        ["hardlink_db", "hardlink_write", [str(env.paths.db_path), str(env.wt / "hl.db")]],
        ["hardlink_repo_readme", "hardlink_write", [str(env.repo / "README.md"), str(env.wt / "hl.md")]],
        ["symlink_db", "symlink", [str(env.paths.db_path), str(env.wt / "sl.db")]],
        ["junction_data", "junction_write", [str(env.wt / "jd"), str(env.paths.data_dir)]],
        ["junction_sibling", "junction_write", [str(env.wt / "js"), str(sibling)]],
        ["schtasks_create", "run", ["schtasks.exe", "/create", "/tn", task_name, "/tr", "cmd.exe /c exit", "/sc",
                                    "once", "/st", "23:59", "/f"]],
    ]
    readme = _sha(env.repo / "README.md")
    try:
        res = _attack(env, ops, relabel=[str(env.paths.data_dir), str(env.paths.db_path), str(sibling)])
    finally:
        subprocess.run(["schtasks.exe", "/delete", "/tn", task_name, "/f"], capture_output=True)
    breakaway = res.pop("breakaway")
    for key in ("relabel_0", "relabel_1", "relabel_2", "raise_own_token", "steal_parent_token"):
        assert res[key].startswith("blocked:PermissionError:code="), (key, res[key])
    assert res["raise_own_token"].endswith("code=1314") or res["raise_own_token"].endswith("code=5"), res
    assert all(res[f"relabel_{i}"].endswith("code=5") for i in range(3)), res
    allowed = {k: v for k, v in res.items() if v.startswith("ALLOWED")}
    assert not allowed, allowed
    assert _sha(env.repo / "README.md") == readme
    assert not (sibling / "pwn.txt").exists() and not (env.paths.data_dir / "pwn.txt").exists()
    assert sandbox.check_data_dir_label(env.paths.data_dir) == []
    assert sandbox.read_label(env.paths.db_path)[0] == sandbox.MEDIUM_RID
    # Breaking away from the job is refused, or (via the venv launcher's own nested job) the child stays in the
    # worker job and at Low integrity; either way it was killed with the command's job.
    if breakaway.startswith("ALLOWED:"):
        pid = int(breakaway.split(":", 1)[1])
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    else:
        assert breakaway.startswith("blocked:")


@windows_only
def test_preplanted_junctions_are_refused_at_preflight(env: Env):
    """Preflight refusal only; this does not establish post-launch containment."""
    sibling = env.paths.worktrees_dir / "sibling"
    sibling.mkdir()
    planted = [(env.wt / "pre_jd", env.paths.data_dir), (env.wt / "pre_js", sibling)]
    marker = env.wt / "preflight-marker"
    try:
        for link, target in planted:
            subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, check=True)
        status, results = env.run([[PY, "-c", "from pathlib import Path; Path('preflight-marker').write_text('unexpected')"]])
        assert status == "error", status
        assert results and all(verification.is_security_error(r) for r in results), results
        assert all("unsafe worktree" in str(r.error).lower() for r in results), results
        assert not marker.exists()
    finally:
        for link, _ in planted:
            if link.is_junction():
                link.rmdir()  # Remove only this test's junction, never its target.


@windows_only
def test_unstaged_powershell_broker_command_is_refused(env: Env):
    """WMI Win32_Process.Create would start a process outside the job at Medium integrity (a broker escape)."""
    ps = shutil.which("powershell.exe")
    if ps is None:
        pytest.skip("powershell not available")
    marker = env.root / "fake-profile-wmi-marker.txt"
    cmd = (f"try {{ $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
           f"@{{CommandLine='cmd.exe /c echo x > \"{marker}\"'}} -ErrorAction Stop; 'RV=' + $r.ReturnValue }} "
           f"catch {{ 'REFUSED ' + $_.Exception.GetType().Name }}")
    try:
        status, results = env.run([[ps, "-NoProfile", "-NonInteractive", "-Command", cmd]])
        time.sleep(1.0)
        if status == "error":
            assert results[0].error.startswith("sandbox_unavailable:"), results
        else:
            assert "REFUSED" in results[0].output, results[0].output
        assert not marker.exists()
    finally:
        if marker.exists():
            marker.unlink()


@windows_only
def test_appcontainer_cannot_write_other_task_worktrees(env: Env):
    """The Low label alone was insufficient; the exact task SID denies this write."""
    other = Path(worktrees.create(env.repo, env.paths.worktrees_dir / "task2", "task2", env.base).path)
    res = _attack(env, [["other_worktree", "write_new", str(other / "pwn.txt")]])
    assert res["other_worktree"].startswith("blocked:"), res


@windows_only
@pytest.mark.parametrize("network", [False, True], ids=["files-and-imports", "network-denial"])
def test_appcontainer_files_network_and_project_imports(env: Env, network):
    import socket

    other = Path(worktrees.create(env.repo, env.paths.worktrees_dir / "task2", "task2", env.base).path)
    (other / "private.txt").write_text("other task")
    own_real = env.wt / "README.md"  # this task's REAL worktree: the command only ever sees its copy
    outside = env.root / "fake-credentials"
    outside.mkdir()
    secrets = [outside / name for name in ("orchestrator.db", "api-token", "credentials.json")]
    for p in secrets:
        p.write_text("FAKE-TEST-DATA")
    (env.wt / "sibling.py").write_text("value = 42\n")
    script = env.wt / "probe.py"
    script.write_text('''
import ctypes, json, os, pathlib, socket, sys
from concurrent.futures import ThreadPoolExecutor
import sibling, pytest
assert sibling.value == 42
assert "verification-runtimes" in sys.executable
assert "verification-runtimes" in pytest.__file__
try:
    open(sys.executable, "ab").write(b"must-not-modify-runtime")
except PermissionError:
    pass
else:
    raise AssertionError("runtime was writable")
spec = json.loads(sys.argv[1])
results = {}
for path in spec["denied"]:
    for mode in ("rb", "ab"):
        try:
            with open(path, mode) as f:
                f.read(1) if mode == "rb" else f.write(b"hostile")
            results[path + mode] = "ALLOWED"
        except PermissionError:
            results[path + mode] = "BLOCKED"
for path in spec["outside_new"]:
    try:
        open(path, "wb").write(b"hostile")
        results[path] = "ALLOWED"
    except PermissionError:
        results[path] = "BLOCKED"
def probe_address(address):
    with socket.socket() as s:
        s.settimeout(2)
        code = s.connect_ex((address, spec["port"]))
        firewall = ctypes.WinDLL("FirewallAPI.dll", use_last_error=True)
        diagnose = firewall.NetworkIsolationDiagnoseConnectFailureAndGetInfo
        diagnose.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        diagnose.restype = ctypes.c_uint32
        missing = ctypes.c_int(-1)
        diagnostic = diagnose(address, ctypes.byref(missing))
        return address, {"connect": code, "diagnostic": diagnostic, "missing_capability": missing.value}
# Independent negative controls share one bounded wait window. Serial two-second
# socket waits plus full runtime validation can exceed the unchanged 12s budget.
if spec["addresses"]:
    with ThreadPoolExecutor(max_workers=3) as pool:
        results.update(pool.map(probe_address, spec["addresses"]))
pathlib.Path("own-write.txt").write_text("ok")
(pathlib.Path(os.environ["TEMP"]) / "own-temp.txt").write_text("ok")
print("APP-CONTAINER-RESULT=" + json.dumps(results))
''')
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen(4)
        host = socket.gethostbyname(socket.gethostname())
        assert not host.startswith("127."), "native test requires a host LAN address"
        # Prove these exact endpoints are live and reachable outside the sandbox.
        # A timeout to an unreachable endpoint would not demonstrate isolation.
        for address in ("127.0.0.1", host):
            with socket.create_connection((address, listener.getsockname()[1]), timeout=2):
                peer, _ = listener.accept()
                peer.close()
        spec = {"denied": [str(p) for p in [other / "private.txt", own_real, *secrets]],
                "outside_new": [str(other / "new.txt"), str(outside / "new.txt"), str(env.wt / "new.txt")],
                "addresses": ["127.0.0.1", host, "192.0.2.1"] if network else [],
                "port": listener.getsockname()[1]}
        status, results = env.run([{"cmd": [PY, str(script), json.dumps(spec)], "timeout": 12}])
        assert status == "passed", (results[0].error, results[0].output)
        output = results[0].output.split("APP-CONTAINER-RESULT=", 1)[1].splitlines()[0]
        observed = json.loads(output)
        for address in spec["addresses"]:
            item = observed[address]
            assert item["diagnostic"] == 0, item
            if address == "192.0.2.1":
                assert item["connect"] == 10013 and item["missing_capability"] in (1, 2, 3), item
            else:
                # WFP may silently DROP a denied flow (Microsoft UWP firewall
                # troubleshooting docs), producing WSAEWOULDBLOCK on timeout.
                # Accept this only against the host-reachable listener, with
                # zero actual capabilities and no loopback exemption verified
                # by production code. LAN denial also needs a native diagnosis.
                assert item["connect"] in (10013, 10035), item
                if address == host:
                    assert item["missing_capability"] in (1, 2, 3), item
        if network:
            from orchestrator.security import appcontainer
            assert appcontainer.task_sid(env.wt) not in appcontainer.loopback_exemptions()
            listener.settimeout(0.2)
            with pytest.raises(TimeoutError):
                listener.accept()  # no sandbox flow completed a handshake
        for address in spec["addresses"]:
            observed.pop(address)  # WSAEACCES only, never timeout/refused
        assert set(observed.values()) == {"BLOCKED"}, observed
    assert not (env.wt / "own-write.txt").exists() and not (env.wt / "new.txt").exists()  # the copy only
    assert own_real.read_text() == "base\n"
    assert all(p.read_text() == "FAKE-TEST-DATA" for p in secrets)
    assert (other / "private.txt").read_text() == "other task"
    assert list((env.paths.sandbox_root("proj1") / "runs").iterdir()) == []  # copy, scratch and caches removed


@windows_only
def test_appcontainer_pytest_explicit_file_and_conftest(env: Env):
    (env.wt / "sibling.py").write_text("value = 42\n")
    conftest = env.wt / "conftest.py"
    conftest.write_text("import pytest, sibling\n@pytest.fixture\ndef answer(): return sibling.value\n")
    (env.wt / "test_private_runtime.py").write_text(
        "import sibling\ndef test_project(answer, tmp_path):\n"
        "    assert sibling.value == answer\n    (tmp_path / 'ok').write_text('ok')\n")
    status, results = env.run([[PY, "-m", "pytest", "-q", "test_private_runtime.py"]])
    assert status == "passed", (results[0].error, results[0].output)
    assert "1 passed" in results[0].output
    conftest.write_text("import pytest, sibling\n@pytest.fixture\ndef answer(): return sibling.value + 1\n")
    status, results = env.run([[PY, "-m", "pytest", "-q", "test_private_runtime.py"]])
    assert status == "failed", results[0].output
    assert results[0].exit_code == 1
    assert "1 failed" in results[0].output


@windows_only
def test_appcontainer_cannot_reacl_other_task_owned_objects(env: Env):
    from orchestrator.security import appcontainer, acl

    other = Path(worktrees.create(env.repo, env.paths.worktrees_dir / "task2", "task2", env.base).path)
    folder = other / "private-dir"
    folder.mkdir()
    victim = folder / "private.txt"
    victim.write_text("unchanged")
    targets = [other, folder, victim]
    before = {str(p): acl.read_security(p) for p in targets}
    script = env.wt / "acl-attack.py"
    script.write_text(r'''
import ctypes, json, sys
from ctypes import wintypes as w
k = ctypes.WinDLL("kernel32", use_last_error=True)
a = ctypes.WinDLL("advapi32", use_last_error=True)
k.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.CloseHandle.argtypes = [w.HANDLE]
a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR,w.DWORD,ctypes.c_void_p,ctypes.c_void_p]
a.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
a.SetNamedSecurityInfoW.argtypes = [w.LPWSTR,ctypes.c_int,w.DWORD,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
a.SetNamedSecurityInfoW.restype = w.DWORD
k.LocalFree.argtypes = [ctypes.c_void_p]
spec = json.loads(sys.argv[1])
sd = ctypes.c_void_p()
assert a.ConvertStringSecurityDescriptorToSecurityDescriptorW(spec["sddl"],1,ctypes.byref(sd),None)
present, defaulted, dacl = w.BOOL(), w.BOOL(), ctypes.c_void_p()
assert a.GetSecurityDescriptorDacl(sd,ctypes.byref(present),ctypes.byref(dacl),ctypes.byref(defaulted))
for path in spec["paths"]:
    for access in (0x40000,0x80000):
        h = k.CreateFileW(path,access,7,None,3,0x02000000|0x00200000,None)
        if h != ctypes.c_void_p(-1).value:
            if h: k.CloseHandle(h)
            raise AssertionError("ownership access granted: " + path)
        assert ctypes.get_last_error() == 5
    result = a.SetNamedSecurityInfoW(path,1,4,None,None,dacl,None)
    assert result == 5, (path,result)
k.LocalFree(sd)
print("REACL-DENIED")
''')
    spec = {"paths": [str(p) for p in targets],
            "sddl": f"D:P(A;;GA;;;{appcontainer.task_sid(env.wt)})(A;;GA;;;{acl.current_user_sid()})"}
    status, results = env.run([[PY, str(script), json.dumps(spec)]])
    assert status == "passed", results[0].output
    assert "REACL-DENIED" in results[0].output
    assert {str(p): acl.read_security(p) for p in targets} == before
    assert victim.read_text() == "unchanged"


@windows_only
@pytest.mark.parametrize("failure", ["profile", "runtime", "acl", "sid", "query", "capabilities",
                                      "capabilities-nonzero", "not-appcontainer", "spawn"])
def test_appcontainer_failures_are_security_results_before_execution(env: Env, monkeypatch, failure):
    from orchestrator.security import appcontainer, python_runtime

    marker = env.wt / "must-not-run"
    def refuse(*args, **kwargs):
        raise appcontainer.AppContainerUnavailable("injected " + failure)
    if failure == "profile":
        monkeypatch.setattr(appcontainer.Profile, "create", refuse)
    elif failure == "runtime":
        def bad_runtime(*args, **kwargs):
            raise python_runtime.RuntimeUnavailable("injected runtime")
        monkeypatch.setattr(python_runtime, "verification_argv", bad_runtime)
    elif failure == "acl":
        monkeypatch.setattr(appcontainer, "grant_directory", refuse)
    elif failure == "sid":
        verify = appcontainer.verify_process
        monkeypatch.setattr(appcontainer, "verify_process", lambda pid, expected: verify(pid, "S-1-15-2-1"))
    elif failure in {"query", "capabilities", "capabilities-nonzero", "not-appcontainer"}:
        original = sandbox._token_info
        def query(token, cls):
            if cls == 29 and failure == "not-appcontainer":
                import ctypes
                return ctypes.create_string_buffer(4)
            if cls == 30 and failure == "capabilities-nonzero":
                import ctypes
                return ctypes.create_string_buffer(b"\x01\x00\x00\x00")
            if cls == (31 if failure == "query" else 30):
                raise OSError("injected token query failure")
            return original(token, cls)
        monkeypatch.setattr(sandbox, "_token_info", query)
    else:
        def bad_spawn(*args, **kwargs):
            raise OSError("injected process attribute failure")
        monkeypatch.setattr(sandbox, "spawn", bad_spawn)
    status, results = env.run([[PY, "-c", f"open({str(marker)!r},'w').write('hostile')"]])
    assert status == "error", results
    assert results[0].error.startswith("sandbox_unavailable:"), results[0].error
    assert verification.is_security_error(results[0])
    assert not marker.exists()


@windows_only
def test_appcontainer_cancellation_is_not_converted_to_security_error(env: Env, monkeypatch):
    from orchestrator.security import appcontainer

    marker = env.wt / "cancelled-must-not-run"
    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(appcontainer, "verify_process", cancel)
    with pytest.raises(asyncio.CancelledError):
        env.run([[PY, "-c", f"open({str(marker)!r},'w').write('hostile')"]])
    assert not marker.exists()


@windows_only
def test_gitlink_tampering_is_detected_and_restored(env: Env):
    link = env.wt / ".git"
    original = link.read_bytes()
    # The command can only reach the `.git` link of its own copy; rewriting it is still a blocking finding.
    code = ("import os; os.makedirs('evil-gitdir', exist_ok=True); os.remove('.git'); "
            "open('.git','w').write('gitdir: evil-gitdir\\n')")
    status, results = env.run([[PY, "-c", code]])
    assert status == "error" and not results[0].passed  # a security error: the task is blocked (N2 re-review)
    assert results[0].error.startswith("sandbox_tamper:") and ".git link" in results[0].error
    assert link.read_bytes() == original and not (env.wt / "evil-gitdir").exists()
    assert git.rev_parse(env.wt) is not None


@windows_only
def test_real_pytest_in_a_worktree_passes_fails_and_the_orchestrator_can_commit_and_clean(env: Env):
    (env.wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (env.wt / "test_calc.py").write_text(
        "import calc\n\ndef test_add(tmp_path):\n    (tmp_path / 'f.txt').write_text('ok')\n    assert calc.add(1, 2) == 3\n"
    )
    t0 = time.perf_counter()
    status, results = env.run([[PY, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:asyncio"]])
    elapsed = time.perf_counter() - t0
    assert status == "passed", results[0].output
    assert "1 passed" in results[0].output
    assert not (env.wt / "__pycache__").exists()
    assert not (env.wt / ".pytest_cache").exists()
    print(f"sandboxed pytest wall time {elapsed:.2f}s")
    (env.wt / "test_calc.py").write_text("import calc\n\ndef test_add():\n    assert calc.add(1, 2) == 4\n")
    status, results = env.run([[PY, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:asyncio"]])
    assert status == "failed" and results[0].exit_code == 1 and "1 failed" in results[0].output
    # Files created by the Low process: the Medium orchestrator commits, diffs, resets and removes them.
    (env.wt / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    sha = git.commit_all(env.wt, "work")
    assert sha and "calc.py" in git.changed_files(env.wt, env.base)
    git.reset_hard(env.wt, env.base)
    assert not (env.wt / "calc.py").exists()
    worktrees.remove(env.repo, env.wt)
    assert not env.wt.exists()


@windows_only
def test_timeout_kills_the_whole_sandboxed_tree(env: Env):
    code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
            "print('GRANDCHILD=%d' % p.pid, flush=True); time.sleep(120)")
    # The deadline includes full runtime byte/ACL validation before spawn. Allow setup
    # time so this specifically tests killing an existing child tree, not startup expiry.
    # The separate native-lifetime regression verifies setup never resets the deadline.
    status, results = env.run([{"cmd": [PY, "-c", code], "timeout": 20}])
    assert status == "failed" and results[0].timed_out
    assert "GRANDCHILD=" in results[0].output, (results[0].error, results[0].duration_seconds, results[0].output)
    pid = int(results[0].output.split("GRANDCHILD=", 1)[1].split()[0])
    deadline = time.time() + 10
    while psutil.pid_exists(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not psutil.pid_exists(pid)


@windows_only
def test_private_npm_executes_project_script_and_preserves_failure(env: Env):
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    assert npm is not None and shutil.which("node") is not None
    (env.wt / "package.json").write_text(json.dumps({"name": "t", "version": "1.0.0",
        "scripts": {"test": "node t.js", "fail": "node -e \"process.exit(7)\""}}))
    (env.wt / "t.js").write_text("require('fs').writeFileSync('node-out.txt', 'ok'); console.log('node-ran')\n")
    policies = effective_policies({})
    assert "PATH" in [k.upper() for k in policies.child_env_allowlist]
    status, results = env.run([["npm", "test"]])
    assert status == "passed", results[0].error or results[0].output
    assert "node-ran" in results[0].output
    assert not (env.wt / "node-out.txt").exists()  # written into the disposable copy, never the worktree
    status, results = env.run([["npm", "run", "fail"]])
    assert status == "failed" and results[0].exit_code == 7, results


@windows_only
def test_in_place_folder_is_read_only_and_not_relabelled(env: Env, tmp_path: Path):
    folder = tmp_path / "user-project"
    folder.mkdir()
    (folder / "test_x.py").write_text("def test_x(tmp_path):\n    (tmp_path / 'a').write_text('1')\n")
    status, results = env.run([[PY, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:asyncio"]], cwd=folder)
    assert status == "error" and results[0].error.startswith("sandbox_unavailable:")
    status, results = env.run([[PY, "-c", "open('out.txt','w').write('x')"]], cwd=folder)
    assert status == "error" and results[0].error.startswith("sandbox_unavailable:")
    assert not (folder / "out.txt").exists()
    assert sandbox.read_label(folder) is None  # never relabelled


@windows_only
def test_fail_closed_when_the_low_token_cannot_be_created(env: Env, monkeypatch):
    marker = env.wt / "ran.txt"

    def broken() -> int:
        raise OSError(5, "simulated token failure")

    monkeypatch.setattr(sandbox, "_create_low_token", broken)
    status, results = env.run([[PY, "-c", f"open(r'{marker}','w').write('x')"]])
    assert status == "error"
    assert results[0].error.startswith("sandbox_unavailable:")
    time.sleep(0.3)
    assert not marker.exists()


@windows_only
def test_fail_closed_when_labelling_fails(env: Env, monkeypatch):
    marker = env.wt / "ran.txt"

    def broken(path: str, sddl: str, **_kw) -> None:
        raise OSError(5, "simulated label failure")

    monkeypatch.setattr(sandbox, "_apply_label", broken)
    r = asyncio.run(run_command([PY, "-c", f"open(r'{marker}','w').write('x')"], str(env.wt), 30, dict(os.environ),
                                writable_cwd=True))
    assert not r.passed and r.error.startswith("sandbox_unavailable:")
    assert not marker.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX behaviour")
def test_posix_fails_closed(tmp_path: Path):
    marker = tmp_path / "ran"
    r = asyncio.run(run_command([PY, "-c", f"open({str(marker)!r},'w').write('x')"], str(tmp_path), 30, dict(os.environ)))
    assert not r.passed and r.error.startswith("sandbox_unavailable:")
    assert not marker.exists()


@windows_only
def test_data_dir_label_low_cannot_read_medium_can(env: Env):
    assert sandbox.check_data_dir_label(env.paths.data_dir) == []
    rid, mask, _ = sandbox.read_label(env.paths.data_dir)
    assert rid == sandbox.MEDIUM_RID and mask == sandbox.NO_WRITE_UP | sandbox.NO_READ_UP | sandbox.NO_EXECUTE_UP
    late = env.paths.data_dir / "created-later.txt"  # inheritance, as for SQLite -wal/-shm files
    late.write_text("x")
    assert sandbox.read_label(late)[0] == sandbox.MEDIUM_RID and sandbox.read_label(late)[1] & sandbox.NO_READ_UP
    res = _attack(env, [["late_read", "read", str(late)], ["token_read", "read", str(env.paths.token_path)]])
    assert set(res) == {"late_read", "token_read", "raise_own_token", "steal_parent_token", "breakaway"}
    assert res["late_read"].startswith("blocked") and res["token_read"].startswith("blocked")
    assert res["raise_own_token"].startswith("blocked") and res["steal_parent_token"].startswith("blocked")
    assert late.read_text() == "x" and env.paths.token_path.read_text(encoding="utf-8").strip() == env.token


@windows_only
def test_ensure_relabels_a_data_dir_without_label(tmp_path: Path):
    data = tmp_path / "d"
    data.mkdir()
    (data / "orchestrator.db").write_bytes(b"x")
    assert sandbox.read_label(data) is None
    get_paths(data, tmp_path / "w").ensure(harden=False)  # also without the ACL switch
    assert sandbox.check_data_dir_label(data) == []
    assert sandbox.read_label(data / "orchestrator.db")[1] & sandbox.NO_READ_UP  # propagated to existing files


def test_child_env_has_no_sandbox_switch():
    env = sandbox.sandbox_env(child_env(["PATH", "TEMP"]), tmp=Path("t"), cache=Path("c"), read_only_cwd=False)
    assert not any("SANDBOX" in k.upper() for k in env)


@windows_only
def test_hardlinks_planted_by_a_medium_agent_are_refused_at_preflight_without_relabel(env: Env, tmp_path: Path):
    """Pre-existing aliases are refused before launch; outside labels/content stay intact."""
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir()
    victim.write_text("v")
    links = [env.wt / "planted-db", env.wt / "planted-victim"]
    created = []
    db_hash = _sha(env.paths.db_path)
    try:
        for target, link in zip((env.paths.db_path, victim), links, strict=True):
            os.link(target, link)
            created.append(link)
        status, results = env.run([[PY, "-c", "from pathlib import Path; Path('preflight-marker').write_text('unexpected')"]])
        assert status == "error", status
        assert results and all(verification.is_security_error(r) for r in results), results
        assert all("unsafe worktree" in str(r.error).lower() for r in results), results
        assert not (env.wt / "preflight-marker").exists()
        assert sandbox.read_label(env.paths.db_path)[0] == sandbox.MEDIUM_RID
        assert sandbox.read_label(victim) is None
        assert victim.read_text() == "v" and _sha(env.paths.db_path) == db_hash
    finally:
        for link in created:
            link.unlink()


@windows_only
def test_worktree_is_never_labelled_or_granted_for_the_sandbox(env: Env):
    from orchestrator.security import acl

    def sandbox_reachable(path: Path) -> bool:
        return (sandbox.read_label(path) is not None
                or any((a["sid"] or "").startswith("S-1-15-2-") for a in acl.read_security(path)["aces"] or []))

    assert not sandbox_reachable(env.wt) and not sandbox_reachable(env.wt / "README.md")
    assert sandbox.is_pinned(env.wt) and sandbox.read_label(env.repo / ".git") is None
    status, results = env.run([[PY, "-c", "open('made-by-verification.txt', 'w').write('x')"]])
    assert status == "passed", results
    assert not sandbox_reachable(env.wt) and not (env.wt / "made-by-verification.txt").exists()
    reused = worktrees.create(env.repo, env.wt, "task1", env.base)  # idempotent reuse re-asserts the pin only
    assert not reused.created and not sandbox_reachable(env.wt)


@windows_only
def test_doctor_reports_the_sandbox_and_the_data_dir_label(tmp_path: Path, monkeypatch):
    from orchestrator.cli import doctor

    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ORCHESTRATOR_WORKTREES_DIR", str(tmp_path / "wts"))
    (tmp_path / "data").mkdir()
    checks = {c["check"]: c for c in doctor.sandbox_checks()}
    assert checks["verification sandbox"]["status"] == doctor.OK
    assert checks["data dir integrity label"]["status"] == doctor.WARN  # not labelled yet
    get_paths().ensure(harden=False)
    checks = {c["check"]: c for c in doctor.sandbox_checks()}
    assert checks["data dir integrity label"]["status"] == doctor.OK
