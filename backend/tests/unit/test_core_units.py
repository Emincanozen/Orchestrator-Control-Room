from datetime import timedelta

import pytest

from orchestrator.config import deep_merge, effective_policies
from orchestrator.db.database import Database
from orchestrator.domain.models import Task
from orchestrator.domain.states import TaskState
from orchestrator.providers.base import ErrorCategory, ExecutionResult
from orchestrator.scheduler.supervisor import Supervisor
from orchestrator.scheduler.verification import CommandSpecError, normalize, split_command
from orchestrator.services.agents import WORKING_ACTIVITIES, ActivityError, activity_for
from orchestrator.domain.states import AgentActivity, RunPhase
from orchestrator.util import new_id, now_iso, parse_iso, to_iso, utcnow

S = TaskState
ALL_TABLES = {
    "projects", "objectives", "tasks", "task_dependencies", "runs", "events", "state_transitions", "checkpoints",
    "verifications", "agents", "approvals", "decisions", "routing_decisions", "provider_status", "provider_models",
    "usage_snapshots", "settings", "leadership", "knowledge_docs", "knowledge_fts", "knowledge_vectors", "skills",
    "skill_events",
}


def test_schema_and_pragmas(tmp_path):
    db = Database(tmp_path / "x.db").open()
    try:
        names = db.read_sync(lambda tx: {r[0] for r in tx.all("SELECT name FROM sqlite_master WHERE type IN ('table')")})
        assert ALL_TABLES <= names
        assert db.schema_version >= 1
        assert db.read_sync(lambda tx: tx.scalar("PRAGMA journal_mode")) == "wal"
        assert db.read_sync(lambda tx: tx.scalar("PRAGMA foreign_keys")) == 1
        # FTS5 works through the triggers
        db.write_sync(lambda tx: tx.execute(
            "INSERT INTO knowledge_docs (id, kind, title, body, source_hash, updated_at) VALUES ('k1','doc','Alpha','worktree isolation','h','t')"
        ))
        hit = db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'worktree'"))
        assert hit == 1
    finally:
        db.close()
    db2 = Database(tmp_path / "x.db").open()  # reopening does not re-run migrations
    assert db2.schema_version >= 1
    db2.close()


def test_write_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "y.db").open()

    def boom(tx):
        tx.execute("INSERT INTO settings (key, value, updated_at) VALUES ('a','1','t')")
        raise ValueError("boom")

    with pytest.raises(ValueError):
        db.write_sync(boom)
    assert db.read_sync(lambda tx: tx.scalar("SELECT COUNT(*) FROM settings")) == 0
    db.close()


def test_ids_are_prefixed_and_unique():
    ids = {new_id("tsk") for _ in range(2000)}
    assert len(ids) == 2000
    assert all(i.startswith("tsk_") and len(i) == 30 for i in ids)


def test_policy_overlay():
    p = effective_policies({"limits": {"max_parallel_workers": 1}})
    assert p.int_limit("max_parallel_workers") == 1
    assert p.int_limit("max_parallel_per_project") == 2  # yaml default kept
    assert "git_init_in_project" in p.approvals_required
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


# ---- command splitting --------------------------------------------------------


@pytest.mark.parametrize(
    "cmd,argv",
    [
        ('python -m pytest -q', ["python", "-m", "pytest", "-q"]),
        ('"C:\\Program Files\\x.exe" --flag', ["C:\\Program Files\\x.exe", "--flag"]),
        ('a "b c" d', ["a", "b c", "d"]),
        ('a\\\\b c', ["a\\\\b", "c"]),
        ('a\\"b', ['a"b']),
        ('a\\\\"b c"', ['a\\b c']),
        ('"a""b"', ['a"b']),
        ("'single' quoted", ["'single'", "quoted"]),
    ],
)
def test_split_windows(cmd, argv):
    assert split_command(cmd, windows=True) == argv


def test_split_posix():
    assert split_command("echo 'a b' \"c\"", windows=False) == ["echo", "a b", "c"]


@pytest.mark.parametrize("cmd", ["a && b", "a | b", "a > out", "a; b", "a `b`", "echo $(id)", "a\nb", '"unbalanced', "   "])
def test_split_rejects(cmd):
    with pytest.raises(CommandSpecError):
        split_command(cmd, windows=True)


def test_quoted_operators_allowed():
    assert split_command('python -c "print(1|2)"', windows=True) == ["python", "-c", "print(1|2)"]


def test_normalize_forms():
    checks = normalize([["a", "b"], "c d", {"cmd": "e f", "timeout": 5, "name": "x"}, {"file_exists": "out.txt"}, {"bogus": 1}, "a && b"])
    assert checks[0].argv == ["a", "b"]
    assert checks[1].argv == ["c", "d"]
    assert checks[2].argv == ["e", "f"] and checks[2].timeout == 5 and checks[2].name == "x"
    assert checks[3].file == "out.txt"
    assert checks[4].error and checks[5].error


# ---- supervisor ------------------------------------------------------------------


def _task(**kw) -> Task:
    ts = now_iso()
    return Task(id="tsk_x", project_id="prj_x", title="t", state=S.RUNNING, created_at=ts, updated_at=ts, **kw)


def _fail(cat: ErrorCategory) -> ExecutionResult:
    return ExecutionResult(success=False, error_category=cat, error_message=cat.value)


@pytest.mark.parametrize(
    "cat,state",
    [
        (ErrorCategory.RATE_LIMIT, S.WAITING_FOR_QUOTA),
        (ErrorCategory.QUOTA_EXHAUSTED, S.WAITING_FOR_QUOTA),
        (ErrorCategory.AUTH, S.BLOCKED),
        (ErrorCategory.PERMISSION_DENIED, S.NEEDS_APPROVAL),
        (ErrorCategory.CANCELLED, S.PAUSED),
        (ErrorCategory.INVALID_REQUEST, S.FAILED),
        (ErrorCategory.NETWORK, S.QUEUED),
        (ErrorCategory.TIMEOUT, S.QUEUED),
        (ErrorCategory.CRASHED, S.QUEUED),
        (ErrorCategory.TOOL_FAILURE, S.QUEUED),
        (ErrorCategory.UNKNOWN, S.QUEUED),
    ],
)
def test_supervisor_category_mapping(cat, state):
    sup = Supervisor(effective_policies({}))
    assert sup.on_run_failure(_task(), _fail(cat)).to_state == state


def test_supervisor_retry_limits():
    sup = Supervisor(effective_policies({"limits": {"max_retry_attempts": 2, "max_consecutive_failures": 10}}))
    assert sup.on_run_failure(_task(retry_count=1), _fail(ErrorCategory.NETWORK)).to_state == S.QUEUED
    assert sup.on_run_failure(_task(retry_count=2), _fail(ErrorCategory.NETWORK)).to_state == S.FAILED
    sup2 = Supervisor(effective_policies({"limits": {"max_retry_attempts": 10, "max_consecutive_failures": 2}}))
    assert sup2.on_run_failure(_task(consecutive_failures=1), _fail(ErrorCategory.NETWORK)).to_state == S.FAILED


def test_supervisor_quota_uses_reset_time():
    sup = Supervisor(effective_policies({}))
    reset = to_iso(utcnow() + timedelta(minutes=30))
    d = sup.on_run_failure(_task(), _fail(ErrorCategory.RATE_LIMIT), resets_at=reset)
    assert d.not_before == reset
    far = to_iso(utcnow() + timedelta(days=30))  # implausible reset -> recheck interval
    d2 = sup.on_run_failure(_task(), _fail(ErrorCategory.QUOTA_EXHAUSTED), resets_at=far)
    assert parse_iso(d2.not_before) < utcnow() + timedelta(hours=1)


def test_activity_mapping():
    assert activity_for(S.PLANNING) == AgentActivity.PLANNING
    assert activity_for(S.RUNNING) == AgentActivity.CODING
    assert activity_for(S.RUNNING, task_type="research") == AgentActivity.RESEARCHING
    assert activity_for(S.TESTING) == AgentActivity.TESTING
    assert activity_for(S.REVIEWING) == AgentActivity.REVIEWING
    assert activity_for(S.RUNNING, phase=RunPhase.ADVISE) == AgentActivity.CONSULTING
    assert activity_for(S.QUEUED) == AgentActivity.IDLE
    assert activity_for(S.WAITING_FOR_QUOTA) == AgentActivity.RATE_LIMITED
    assert activity_for(S.NEEDS_APPROVAL) == AgentActivity.NEEDS_APPROVAL
    assert activity_for(S.BLOCKED) == AgentActivity.BLOCKED
    assert activity_for(S.COMPLETED) == AgentActivity.CELEBRATING


async def test_working_activity_requires_live_process(h):
    agents = await h.agents.list()
    assert len(agents) == 10
    assert {a.room for a in agents} <= {"leadership", "backend", "frontend", "research", "testing", "visual", "meeting", "waiting"}
    for act in WORKING_ACTIVITIES:
        with pytest.raises(ActivityError):
            await h.agents.set_activity("agt_backend_1", act)
    a = await h.agents.set_activity("agt_backend_1", AgentActivity.CODING, live=True, run_id="run_x", operation="execute")
    assert a.activity == AgentActivity.CODING and a.current_run_id == "run_x"
    a = await h.agents.set_activity("agt_backend_1", AgentActivity.IDLE)
    assert a.current_run_id is None and a.current_task_id is None
    events = await h.ctx.bus.replay(0, types=["agent.activity_changed"])
    assert [e.payload["to"] for e in events] == ["coding", "idle"]
