import itertools

import pytest

from orchestrator.db import tasks as tasks_repo
from orchestrator.domain.states import ALLOWED_TRANSITIONS, InvalidTransition, TaskState, assert_transition
from orchestrator.errors import Conflict
from orchestrator.services.tasks import transition_tx
from orchestrator.util import new_id, now_iso

S = TaskState
ALL_PAIRS = list(itertools.product(list(S), list(S)))


@pytest.mark.parametrize("current,target", ALL_PAIRS, ids=[f"{a.value}->{b.value}" for a, b in ALL_PAIRS])
def test_assert_transition_matches_table(current, target):
    if target in ALLOWED_TRANSITIONS[current]:
        assert_transition(current, target)
    else:
        with pytest.raises(InvalidTransition):
            assert_transition(current, target)


def test_terminal_states_have_no_exits():
    assert ALLOWED_TRANSITIONS[S.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[S.CANCELLED] == frozenset()


async def test_every_pair_through_transition_function(h):
    """Every legal pair succeeds and writes row+transition+event; every illegal pair changes nothing."""
    p = await h.project()
    task = await h.task(p.id)

    def force(tx, state):  # test-only: put the task into an arbitrary starting state
        tx.execute("UPDATE tasks SET state = ? WHERE id = ?", (state.value, task.id))

    def add_passing_verification(tx):
        ts = now_iso()
        tx.execute(
            "INSERT INTO verifications (id, task_id, status, checks, started_at, finished_at) VALUES (?,?,?,?,?,?)",
            (new_id("vrf"), task.id, "passed", "[]", ts, ts),
        )

    await h.ctx.db.write(add_passing_verification)
    for current, target in ALL_PAIRS:
        await h.ctx.db.write(lambda tx: force(tx, current))
        before = await h.ctx.db.read(
            lambda tx: (tx.scalar("SELECT COUNT(*) FROM state_transitions"), tx.scalar("SELECT COUNT(*) FROM events"))
        )
        legal = target in ALLOWED_TRANSITIONS[current]
        if legal:
            t = await h.ctx.db.write(lambda tx: transition_tx(h.ctx, tx, task.id, target, reason="t", actor="test"))
            assert t.state == target
        else:
            with pytest.raises(InvalidTransition):
                await h.ctx.db.write(lambda tx: transition_tx(h.ctx, tx, task.id, target, actor="test"))
        after = await h.ctx.db.read(
            lambda tx: (tx.scalar("SELECT COUNT(*) FROM state_transitions"), tx.scalar("SELECT COUNT(*) FROM events"))
        )
        delta = (after[0] - before[0], after[1] - before[1])
        assert delta == ((1, 1) if legal else (0, 0)), (current, target)
        stored = await h.ctx.db.read(lambda tx: tasks_repo.get(tx, task.id))
        assert stored.state == (target if legal else current)


async def test_completed_requires_passing_verification(h):
    p = await h.project()
    task = await h.task(p.id)
    await h.ctx.db.write(lambda tx: tx.execute("UPDATE tasks SET state='TESTING' WHERE id=?", (task.id,)))
    with pytest.raises(Conflict) as exc:
        await h.tasks.transition(task.id, S.COMPLETED)
    assert exc.value.code == "verification_required"
    assert (await h.tasks.get(task.id)).state == S.TESTING
