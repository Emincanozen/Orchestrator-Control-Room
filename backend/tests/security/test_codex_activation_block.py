"""Default Codex refusal is a durable task block, never a retry through legacy execution."""

from contract.conftest import BASE_ENV, FakeSpawner, fake_resolver

from orchestrator.domain.states import TaskState
from orchestrator.providers.codex import CodexAdapter


async def test_unactivated_codex_blocks_task_without_process_repair_or_verification(h):
    spawner = FakeSpawner([])
    adapter = CodexAdapter(spawner=spawner, resolver=fake_resolver, environ=dict(BASE_ENV))
    h.executor.providers["mock"] = adapter
    project = await h.project()
    task = await h.task(project.id, verify_commands=[{"file_exists": "result.txt"}], autostart=True)
    assert await h.run_once(task.id) == TaskState.BLOCKED
    current = await h.tasks.get(task.id)
    assert "owned runtime activation" in current.state_reason
    assert not spawner.calls
    runs = await h.ctx.db.read(lambda tx: tx.all("SELECT status FROM runs WHERE task_id = ?", (task.id,)))
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    verifications = await h.ctx.db.read(lambda tx: tx.all("SELECT id FROM verifications WHERE task_id = ?", (task.id,)))
    assert verifications == []
