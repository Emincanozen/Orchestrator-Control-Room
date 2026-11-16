"""Cancellation cleanup failures must survive a successfully closed provider stream."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.config import Policies
from orchestrator.domain.models import Task
from orchestrator.domain.states import TaskState
from orchestrator.gitops import git
from orchestrator.providers.base import AgentEvent, AgentEventKind, ErrorCategory, ExecutionRequest
from orchestrator.scheduler import executor as executor_module
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.supervisor import Supervisor
from orchestrator.security import worker_job


@pytest.fixture
def invocation(tmp_path):
    task = Task(
        id="tsk_cleanup", project_id="prj_cleanup", title="cleanup", state=TaskState.RUNNING,
        created_at="2026-11-14T00:00:00Z", updated_at="2026-11-14T00:00:00Z",
    )
    request = ExecutionRequest(run_id="run_cleanup", prompt="test", cwd=str(tmp_path))
    executor = TaskExecutor(SimpleNamespace(), {})
    return executor, task, request


@pytest.mark.parametrize("trigger", ["runtime", "heartbeat", "cancel"])
@pytest.mark.parametrize("containment_failure", [True, False], ids=["containment-error", "normal-cleanup"])
async def test_cancelled_next_item_cleanup_result(invocation, monkeypatch, trigger, containment_failure):
    executor, task, request = invocation
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def stream():
        try:
            started.set()
            await asyncio.Event().wait()
            yield AgentEvent(kind=AgentEventKind.TEXT, data={"text": "unreachable"})
        finally:
            cleaned.set()
            if containment_failure:
                raise worker_job.ContainmentError("injected unverified descendant exit")

    gen = stream()
    adapter = SimpleNamespace(name="fake", execute=lambda _: gen, cancel=AsyncMock(return_value=False))
    sup = Supervisor(Policies({"limits": {
        "max_active_runtime_seconds": 0.03 if trigger == "runtime" else 30,
        "run_heartbeat_timeout_seconds": 0.03 if trigger == "heartbeat" else 30,
    }}))
    monkeypatch.setattr(executor_module, "STOP_GRACE_SECONDS", 0.03)
    consume = asyncio.create_task(executor._consume(task, request.run_id, adapter, request, sup))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if trigger == "cancel":
            await executor.request_stop(task.id, "cancel")
        with git.worktree_busy(request.cwd):
            result, resets_at, stop_mode = await asyncio.wait_for(consume, 2)
    finally:
        if not consume.done():
            consume.cancel()
        await asyncio.gather(consume, return_exceptions=True)

    assert cleaned.is_set()
    assert gen.ag_frame is None
    await gen.aclose()  # Closing again succeeds even when __anext__ failed during cleanup.
    adapter.cancel.assert_awaited_once_with(request.run_id)
    assert not result.success
    assert resets_at is None
    assert stop_mode == ("cancel" if trigger == "cancel" else None)
    if containment_failure:
        assert result.error_category == ErrorCategory.TOOL_FAILURE
        assert result.usage.get("_containment_error") is True
        assert "injected unverified descendant exit" in result.error_message
        with pytest.raises(git.QuarantinedWorktree), git.worktree_busy(request.cwd):
            pytest.fail("unverified writers must prevent worktree reuse")
    else:
        expected = ErrorCategory.CANCELLED if trigger == "cancel" else ErrorCategory.TIMEOUT
        assert result.error_category == expected
        assert not result.usage.get("_containment_error")
        with git.worktree_busy(request.cwd):
            pass


async def test_pending_next_item_is_not_confirmed_closed(invocation, monkeypatch):
    executor, task, request = invocation
    next_item = asyncio.get_running_loop().create_future()
    next_item.cancel = lambda *args, **kwargs: False
    gen = SimpleNamespace(__anext__=lambda: next_item, aclose=AsyncMock())
    adapter = SimpleNamespace(name="fake", execute=lambda _: gen, cancel=AsyncMock(return_value=False))
    sup = Supervisor(Policies({"limits": {
        "max_active_runtime_seconds": 0.03, "run_heartbeat_timeout_seconds": 30,
    }}))
    real_wait = asyncio.wait

    async def bounded_wait(futures, *, timeout=None, **kwargs):
        if timeout == 5:
            return set(), set(futures)  # The cancelled read has not completed within its cleanup budget.
        return await real_wait(futures, timeout=timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait", bounded_wait)
    try:
        result, _, _ = await asyncio.wait_for(
            executor._consume(task, request.run_id, adapter, request, sup), 2,
        )
        assert not next_item.done()
        assert result.error_category == ErrorCategory.TOOL_FAILURE
        assert result.usage.get("_containment_error") is True
        with pytest.raises(git.QuarantinedWorktree), git.worktree_busy(request.cwd):
            pytest.fail("a pending read cannot prove provider exit")
        gen.aclose.assert_not_awaited()
    finally:
        next_item.set_result(None)
