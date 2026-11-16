"""Preserve native tool results without inferring denial from model prose."""
import json

import pytest

from orchestrator.providers.base import AgentEventKind as K, UnsupportedOperation
from orchestrator.providers.codex import CodexAdapter, parse_codex_line
from orchestrator.providers.process import ParseState
from .conftest import FakeSpawner, collect, make_adapter, request


def parse(state, event, item):
    return parse_codex_line(json.dumps({'type': event, 'item': item}), state)


def test_started_command_keeps_distinct_redacted_failed_completion():
    state = ParseState()
    start = {'id': 'tool-1', 'type': 'command_execution', 'command': 'write marker', 'status': 'in_progress'}
    events = parse(state, 'item.started', start)
    finish = {**start, 'status': 'failed', 'exit_code': 1,
              'aggregated_output': 'Permission denied api_key=FAKE_PRIVATE_VALUE_123456789 ' + 'x' * 2000}
    events += parse(state, 'item.completed', finish)
    tools = [e.data for e in events if e.kind == K.TOOL_USE]
    assert [t['stage'] for t in tools] == ['started', 'completed']
    assert tools[-1]['status'] == 'failed' and tools[-1]['exit_code'] == 1
    assert tools[0]['tool_call_id'] == tools[1]['tool_call_id'] == 'tool-1'
    assert 'Permission denied' in tools[-1]['diagnostic']
    assert 'FAKE_PRIVATE_VALUE' not in tools[-1]['diagnostic']
    assert len(tools[-1]['diagnostic']) <= 1000
    assert state.permission_denials == 0  # Generic tool failure is not a permission verdict.
    assert not parse(state, 'item.completed', finish), 'duplicate terminal events must not duplicate telemetry'


def test_failed_file_change_is_observed_without_being_reported_as_a_success():
    state = ParseState()
    events = parse(state, 'item.completed', {'id': 'patch-1', 'type': 'file_change',
        'status': 'failed', 'changes': [{'path': 'answer.txt', 'kind': 'add'}],
        'error': {'message': 'write denied', 'code': 'permission_denied'}})
    data = events[0].data
    assert data['stage'] == 'completed' and data['status'] == 'failed'
    assert data['files'] == ['answer.txt'] and data['diagnostic'] == 'write denied'


def test_completion_without_start_and_missing_ids_remain_observable():
    state = ParseState()
    for name in ('one', 'two'):
        events = parse(state, 'item.completed', {'type': 'command_execution', 'command': name,
                                                'status': 'completed', 'exit_code': 0})
        assert events[0].data['stage'] == 'completed'
        assert events[0].data['exit_code'] == 0


def test_completed_process_with_nonzero_exit_preserves_redacted_diagnostic():
    state = ParseState()
    event = parse(state, 'item.completed', {'id': 'exit-1', 'type': 'command_execution',
        'status': 'completed', 'exit_code': 1, 'aggregated_output': 'script failed password=FAKE_PASSWORD_123456'})[0]
    assert event.data['exit_code'] == 1
    assert event.data['diagnostic'].startswith('script failed password=')
    assert 'FAKE_PASSWORD_123456' not in event.data['diagnostic']
    assert state.permission_denials == 0


async def test_recovered_intermediate_error_does_not_override_completed_model_turn():
    lines = [json.dumps(x) for x in [
        {'type': 'item.started', 'item': {'id': 'a', 'type': 'command_execution', 'status': 'in_progress'}},
        {'type': 'item.completed', 'item': {'id': 'a', 'type': 'command_execution', 'status': 'failed', 'exit_code': 1}},
        {'type': 'item.completed', 'item': {'id': 'b', 'type': 'command_execution', 'status': 'completed', 'exit_code': 0}},
        {'type': 'turn.completed', 'usage': {}}]]
    events = await collect(make_adapter('codex', FakeSpawner(lines)), request())
    assert events[-1].data['success'] is True
    assert [e.data['exit_code'] for e in events if e.kind == K.TOOL_USE and e.data.get('stage') == 'completed'] == [1, 0]


async def test_readonly_claim_without_actual_tool_denial_is_not_a_permission_verdict():
    lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'workspace read-only'}}),
             json.dumps({'type': 'turn.completed', 'usage': {}})]
    events = await collect(make_adapter('codex', FakeSpawner(lines)), request())
    assert events[-1].data['success'] is True and events[-1].data['error_category'] is None
    assert not any(e.kind == K.TOOL_USE for e in events)


def test_resume_rejects_additional_directories_instead_of_silently_dropping_them():
    with pytest.raises(UnsupportedOperation, match='resume.*extra_dirs'):
        CodexAdapter().build_command(request(resume_session_id='session-1', extra_dirs=['C:/extra']))


async def test_unsupported_resume_scope_never_spawns_provider():
    spawner = FakeSpawner([])
    adapter = make_adapter('codex', spawner)
    events = await collect(adapter, request(resume_session_id='session-1', extra_dirs=['C:/extra']))
    assert not spawner.calls
    assert events[-1].data['success'] is False and events[-1].data['error_category'] == 'invalid_request'
