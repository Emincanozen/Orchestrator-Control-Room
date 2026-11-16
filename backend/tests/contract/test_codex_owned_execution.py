"""Owned production execution flow with synthetic contexts/processes only."""

import asyncio
import threading

import pytest

from orchestrator.providers import codex as mod
from orchestrator.providers import process as process_module
from orchestrator.providers.base import AgentEventKind
from orchestrator.security.codex_runtime import CodexRuntimeCancelled, CodexRuntimeUnavailable
from orchestrator.security.worker_job import ContainmentError

from .conftest import FakeProcess, FakeSpawner, make_adapter, request


class Context:
    def __init__(self, home, log):
        self.home = home
        self.log = log
        self.veto = False
        self.environment = {"CODEX_HOME": str(home), "PATH": "synthetic-owned-path"}

    def prepare(self, snapshot):
        assert snapshot == b"synthetic-snapshot"
        self.log.append("prepare")

    def validate_before_launch(self):
        if self.veto:
            raise CodexRuntimeCancelled("synthetic veto")
        self.log.append("validate")

    def cancel_pending_start(self):
        self.veto = True

    def close(self, retain=False):
        self.log.append("retain" if retain else "close")


def configured(tmp_path, monkeypatch, *, spawner=None):
    log = []
    ctx = Context(tmp_path, log)
    adapter = make_adapter("codex", spawner or FakeSpawner(['{"type":"turn.completed"}']))
    adapter._new_owned_context = lambda req: ctx
    # Use the actual production preparation method, substituting only auth I/O.
    adapter._prepare_owned_context = mod.CodexAdapter._prepare_owned_context.__get__(adapter)
    monkeypatch.setattr(mod, "read_subscription_auth", lambda env: b"synthetic-snapshot")

    async def inventory(prefix, env, **kwargs):
        assert env == ctx.environment
        log.append("inventory")
        return ()

    adapter._mcp_inventory = inventory
    return adapter, ctx, log


@pytest.mark.parametrize("permission,name", [("read_only", "orch-read"), ("workspace_write", "orch-write")])
@pytest.mark.parametrize("resume", [None, "session-1"])
async def test_owned_exec_and_resume_use_named_permissions_and_cleanup_before_result(tmp_path, monkeypatch, permission, name, resume):
    spawner = FakeSpawner(['{"type":"turn.completed"}'])
    adapter, ctx, log = configured(tmp_path, monkeypatch, spawner=spawner)
    req = request(permission=permission, resume_session_id=resume)
    events = []
    async for event in adapter.execute(req):
        if event.kind == AgentEventKind.RESULT:
            assert log[-1] == "close"
        events.append(event)
    assert events[-1].data["success"] is True
    call = spawner.calls[0]
    assert call["env"] == ctx.environment
    argv = call["argv"]
    assert f'default_permissions="{name}"' in argv and 'windows.sandbox="mxc"' in argv
    assert "-s" not in argv and not any("sandbox_mode=" in item for item in argv)
    assert ("resume" in argv) == bool(resume)
    assert call["cwd"] == req.cwd
    assert log == ["prepare", "validate", "inventory", "validate", "close"]
    assert not adapter._owned_contexts


@pytest.mark.parametrize("inventory", [None, ("unwanted",)])
async def test_inventory_failure_cleans_context_without_launch(tmp_path, monkeypatch, inventory):
    spawner = FakeSpawner([])
    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawner)

    async def inspect(*args, **kwargs):
        return inventory

    adapter._mcp_inventory = inspect
    events = [event async for event in adapter.execute(request())]
    assert not spawner.calls and log[-1] == "close"
    assert events[-1].data["usage"]["_isolation_error"] is True


async def test_uncertain_provider_drain_retains_context_and_never_emits_success(tmp_path, monkeypatch):
    class Undrained(FakeProcess):
        async def terminate_tree(self):
            raise ContainmentError("synthetic unknown drain")

    async def spawn(*args, **kwargs):
        return Undrained(['{"type":"turn.completed"}'])

    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawn)
    events = []
    with pytest.raises(ContainmentError):
        async for event in adapter.execute(request()):
            events.append(event)
    assert log[-1] == "retain"
    assert not any(event.kind == AgentEventKind.RESULT for event in events)


async def test_cleanup_failure_is_containment_error_not_success(tmp_path, monkeypatch):
    adapter, ctx, _ = configured(tmp_path, monkeypatch)

    def fail(retain=False):
        raise CodexRuntimeUnavailable("synthetic cleanup uncertainty")

    ctx.close = fail
    with pytest.raises(ContainmentError, match="cleanup uncertain"):
        [event async for event in adapter.execute(request())]


async def test_prepare_cancellation_joins_thread_before_cleanup(tmp_path, monkeypatch):
    adapter, ctx, log = configured(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def prepare(snapshot):
        entered.set()
        assert release.wait(5), "test release missing"
        log.append("prepare-finished")
        if ctx.veto:
            raise CodexRuntimeCancelled("synthetic cancelled prepare")

    ctx.prepare = prepare
    stream = adapter.execute(request())
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert "close" not in log
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctx.veto and log == ["prepare-finished", "close"]
        assert not adapter._owned_contexts
    finally:
        release.set()
        await stream.aclose()


async def test_explicit_cancel_before_process_registration_prevents_spawn(tmp_path, monkeypatch):
    spawner = FakeSpawner([])
    adapter, ctx, log = configured(tmp_path, monkeypatch, spawner=spawner)

    async def inspect(*args, **kwargs):
        assert await adapter.cancel("run-1") is True
        return ()

    adapter._mcp_inventory = inspect
    events = [event async for event in adapter.execute(request())]
    assert ctx.veto and not spawner.calls and log[-1] == "close"
    assert events[-1].data["success"] is False


async def test_owned_schema_uses_home_not_environment_temp_override(tmp_path, monkeypatch):
    spawner = FakeSpawner(['{"type":"turn.completed"}'])
    adapter, _, _ = configured(tmp_path, monkeypatch, spawner=spawner)
    monkeypatch.setenv(mod.SCHEMA_IN_TEMP_ENV, "1")
    events = [e async for e in adapter.execute(request(json_schema={"type": "object"}))]
    assert events[-1].kind == AgentEventKind.RESULT
    argv = spawner.calls[0]["argv"]
    schema = mod.Path(argv[argv.index("--output-schema") + 1])
    assert schema.parent == tmp_path and not schema.exists()


async def test_interrupted_inventory_retains_leases_and_raises_containment(tmp_path, monkeypatch):
    adapter, _, log = configured(tmp_path, monkeypatch)
    entered = asyncio.Event()

    async def inspect(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    adapter._mcp_inventory = inspect
    stream = adapter.execute(request())
    task = asyncio.create_task(anext(stream))
    await entered.wait()
    task.cancel()
    with pytest.raises(ContainmentError, match="retained"):
        await task
    assert log[-1] == "retain" and "close" not in log
    await stream.aclose()


async def test_cancel_while_spawn_pending_cancels_creation_before_return(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    proc = FakeProcess(['{"type":"turn.completed"}'])
    aborted = False

    async def spawn(*args, **kwargs):
        nonlocal aborted
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            aborted = True
            raise
        return proc

    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawn)
    stream = adapter.execute(request())
    task = asyncio.create_task(anext(stream))
    await entered.wait()
    assert await adapter.cancel("run-1") is True
    release.set()
    event = await task
    # Base emits ERROR before its final RESULT for a cancelled process.
    remaining = [event, *[e async for e in stream]]
    assert remaining[-1].data["error_category"] == "cancelled"
    assert aborted and not proc.killed and log[-1] == "close" and not adapter._cancelled
    assert not adapter._spawns


async def test_abandoned_text_stream_drains_before_context_close(tmp_path, monkeypatch):
    proc = FakeProcess(['{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}'])
    log = []
    original = proc.terminate_tree

    async def drain():
        log.append("drain")
        return await original()

    proc.terminate_tree = drain

    async def spawn(*args, **kwargs):
        return proc

    adapter, ctx, _ = configured(tmp_path, monkeypatch, spawner=spawn)
    ctx.log = log
    stream = adapter.execute(request())
    assert (await anext(stream)).kind == AgentEventKind.TEXT
    await stream.aclose()
    assert log[-2:] == ["drain", "close"] and "retain" not in log


async def test_cleanup_failure_wins_over_concurrent_consumer_cancellation(tmp_path, monkeypatch):
    adapter, ctx, _ = configured(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def cleanup(retain=False):
        entered.set()
        assert release.wait(5)
        raise CodexRuntimeUnavailable("synthetic cleanup failure")

    ctx.close = cleanup
    stream = adapter.execute(request())
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(ContainmentError, match="cleanup uncertain"):
            await task
    finally:
        release.set()
        await stream.aclose()


async def test_explicit_cancellation_during_cleanup_cannot_emit_success(tmp_path, monkeypatch):
    adapter, ctx, log = configured(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def cleanup(retain=False):
        entered.set()
        assert release.wait(5)
        log.append("close")

    ctx.close = cleanup
    stream = adapter.execute(request())
    task = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert await adapter.cancel("run-1") is True
        release.set()
        event = await task
        assert event.kind == AgentEventKind.RESULT
        assert event.data["success"] is False and event.data["error_category"] == "cancelled"
    finally:
        release.set()
        await stream.aclose()


async def test_concurrent_invocations_never_share_owned_environment(tmp_path, monkeypatch):
    spawner = FakeSpawner(['{"type":"turn.completed"}'])
    adapter, _, _ = configured(tmp_path, monkeypatch, spawner=spawner)
    contexts = {name: Context(tmp_path / name, []) for name in ("one", "two")}
    adapter._new_owned_context = lambda req: contexts[req.run_id]

    async def inventory(prefix, env, **kwargs):
        await asyncio.sleep(0)
        assert env in [ctx.environment for ctx in contexts.values()]
        return ()

    adapter._mcp_inventory = inventory

    async def collect(name):
        return [event async for event in adapter.execute(request(run_id=name))]

    results = await asyncio.gather(collect("one"), collect("two"))
    assert all(events[-1].data["success"] for events in results)
    assert {call["env"]["CODEX_HOME"] for call in spawner.calls} == {
        str(tmp_path / "one"), str(tmp_path / "two")}
    assert all(ctx.log[-1] == "close" for ctx in contexts.values())
    assert not adapter._owned_contexts and not adapter._spawns


async def test_completed_spawn_handle_is_adopted_when_consumer_is_interrupted(tmp_path, monkeypatch):
    proc = FakeProcess(['{"type":"turn.completed"}'])
    req = request()
    interrupt = None

    async def cancel_and_interrupt():
        assert await adapter.cancel(req.run_id)
        consumer.cancel()

    async def spawn(*args, **kwargs):
        nonlocal interrupt
        interrupt = asyncio.create_task(cancel_and_interrupt())
        return proc

    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawn)

    async def collect():
        return [event async for event in adapter.execute(req)]

    consumer = asyncio.create_task(collect())
    try:
        events = await consumer
        assert events[-1].data["error_category"] == "cancelled"
        assert proc.killed, "completed spawn handle must not be lost during consumer interruption"
        assert log[-1] == "close" and not adapter._spawns
    finally:
        if interrupt is not None:
            await interrupt


async def test_cancellation_after_preflight_does_not_create_process(tmp_path, monkeypatch):
    spawner = FakeSpawner([])
    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawner)
    original = adapter.before_spawn

    async def preflight(req, spec):
        await original(req, spec)
        assert await adapter.cancel(req.run_id)

    adapter.before_spawn = preflight
    events = [event async for event in adapter.execute(request())]
    assert not spawner.calls and log[-1] == "close"
    assert events[-1].data["error_category"] == "cancelled"


async def test_repeated_cancel_does_not_interrupt_pending_creation_abort(tmp_path, monkeypatch):
    entered, aborting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    interrupts = 0

    async def spawn(*args, **kwargs):
        nonlocal interrupts
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            interrupts += 1
            aborting.set()
            await release.wait()
            raise

    adapter, _, log = configured(tmp_path, monkeypatch, spawner=spawn)
    stream = adapter.execute(request())

    async def collect():
        return [event async for event in stream]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await adapter.cancel("run-1")
        await asyncio.wait_for(aborting.wait(), 2)
        for _ in range(3):
            assert await adapter.cancel("run-1")
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        events = await task
        assert interrupts == 1 and events[-1].data["error_category"] == "cancelled"
        assert log[-1] == "close"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.aclose()


async def test_native_drain_failure_survives_repeated_task_cancellation():
    entered, release = threading.Event(), threading.Event()

    class Job:
        def close(self):
            entered.set()
            assert release.wait(5)
            raise ContainmentError("synthetic native drain failure")

    task = asyncio.create_task(process_module._drain_job(Job()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(ContainmentError, match="synthetic native drain failure"):
            await task
    finally:
        release.set()


@pytest.mark.skipif(not process_module.IS_WINDOWS, reason="Windows native spawn job cleanup")
async def test_original_spawn_containment_error_survives_cancelled_successful_drain(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class Job:
        def close(self):
            entered.set()
            assert release.wait(5)
            return True

    async def create(*args, **kwargs):
        raise ContainmentError("synthetic unresolved creation")

    monkeypatch.setattr(process_module.worker_job, "SpawnJob", Job)
    monkeypatch.setattr(process_module, "_create_child", create)
    task = asyncio.create_task(process_module.ManagedProcess.spawn(["synthetic"], cwd="synthetic", env={}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(ContainmentError, match="synthetic unresolved creation"):
            await task
    finally:
        release.set()


async def test_repeated_cancellation_does_not_reset_native_drain_budget(monkeypatch):
    release = threading.Event()
    budgets = []
    real_wait_for = asyncio.wait_for

    class Job:
        def close(self):
            assert release.wait(5)
            return True

    async def interrupted_wait(awaitable, timeout):
        budgets.append(timeout)
        if len(budgets) < 3:
            await asyncio.sleep(0.04)  # allow the native clock to advance; adjacent readings may be equal
            raise asyncio.CancelledError
        release.set()
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", interrupted_wait)
    try:
        with pytest.raises(asyncio.CancelledError):
            await process_module._drain_job(Job())
        assert len(budgets) == 3
        assert 0 < budgets[2] <= budgets[1] <= budgets[0] <= process_module.worker_job.DRAIN_TIMEOUT + 2
        assert budgets[-1] < budgets[0]  # cancellation must not replenish the initial budget
    finally:
        release.set()
