from orchestrator.testing import py_check


async def test_harness_boots_and_leader_is_set(lh):
    assert await lh.stack.leadership.leader() == ("alpha", "alpha-model")
    cands = await lh.stack.leadership.candidates("leader")
    assert {m["provider"] for m in cands["models"] if m["eligible"]} == {"alpha", "beta"}
