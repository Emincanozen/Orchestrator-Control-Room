"""Suite-wide guard: the app must never build (and so never spawn) the real claude/codex/agy adapters."""

import os

os.environ["ORCHESTRATOR_DISABLE_REAL_PROVIDERS"] = "1"
# Owner-only ACL hardening runs icacls (~0.15 s per call on this machine) for every new data dir; the suite
# creates hundreds. Hardening itself is covered by tests/security/test_sec_08_acl.py, which enables it explicitly.
os.environ["ORCHESTRATOR_HARDEN_ACL"] = "0"
# No background runtime prewarm in tests: native tests acquire the runtime explicitly and measure it.
os.environ["ORCHESTRATOR_NO_RUNTIME_PREWARM"] = "1"

# Starlette's TestClient reports the client host "testclient" (not an IP). The worker-origin guard treats such a
# peer as unidentifiable (no state-changing requests) unless the app trusts the test client; production never
# does. A Python attribute, so it applies to this test process only (re-review N1).
from orchestrator.api import security as _api_security  # noqa: E402

_api_security.TRUST_TEST_CLIENT_DEFAULT = True


import gc  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _prewarm_shared_runtimes(request):
    """Mirror the server's start-up prewarm: when this session runs packages that start real sandboxed
    verification commands, validate the shared runtimes once before any test, so no single test pays the
    cold full validation inside its own command deadline."""
    if os.name == "nt" and any(item.nodeid.startswith(("tests/security/", "tests/integration/", "tests/leader/"))
                               for item in request.session.items):
        from orchestrator.security import sandbox

        sandbox.prewarm_verification_runtimes()
    yield


@pytest.fixture(autouse=True, scope="session")
def _release_shared_runtimes():
    """The managed verification runtime is a process-wide shared lease (security/python_runtime.py), as in
    production: fully validated on first use, then re-checked cheaply on every acquisition while pinned.
    Releasing it after every test would make each native test pay the cold full validation (seconds, and
    much more under file-system load) inside its own command deadline, which production never does (the
    server prewarms it once). Release unreferenced pins once, at the end of the session. Tests that need a
    fresh validation use their own runtime parent (exclusive lease) or call shutdown_shared_runtimes()."""
    yield
    if os.name == "nt":
        from orchestrator.security import python_runtime

        gc.collect()
        python_runtime.shutdown_shared_runtimes()


@pytest.fixture
def native_task(tmp_path):
    """New task-owned directory for native spawn tests, without starting Git."""
    from orchestrator.security import appcontainer, sandbox

    task = tmp_path / "task"
    task.mkdir()
    if sandbox.supported():
        appcontainer.grant_directory(task, appcontainer.task_sid(task), writable=True)
        sandbox.label_low(task)
    (task / ".git").write_text("gitdir: unused-by-spawn-test\n")
    return task
