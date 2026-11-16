"""Server-start runtime prewarm: acquires and releases both shared leases once; never raises."""

from orchestrator.security import node_runtime, python_runtime, sandbox


def _fake(calls, name, fail=False):
    class Lease:
        def acquire(self):
            calls.append((name, "acquire"))
            if fail:
                raise python_runtime.RuntimeUnavailable("synthetic")
            return "exe"

        def close(self):
            calls.append((name, "close"))

    return Lease


def test_prewarm_acquires_and_releases_both_runtimes(monkeypatch):
    calls = []
    monkeypatch.setattr(python_runtime, "RuntimeLease", _fake(calls, "python"))
    monkeypatch.setattr(node_runtime, "NodeRuntimeLease", _fake(calls, "node"))
    sandbox.prewarm_verification_runtimes()
    assert calls == [("python", "acquire"), ("python", "close"), ("node", "acquire"), ("node", "close")]


def test_prewarm_failure_is_logged_not_raised_and_does_not_skip_the_other(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(python_runtime, "RuntimeLease", _fake(calls, "python", fail=True))
    monkeypatch.setattr(node_runtime, "NodeRuntimeLease", _fake(calls, "node"))
    sandbox.prewarm_verification_runtimes()  # must not raise
    assert ("python", "close") in calls and ("node", "acquire") in calls
    assert "prewarm skipped (python)" in caplog.text
