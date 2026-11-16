"""Event types (ARCHITECTURE §5) and the Event envelope."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_STATE_CHANGED = "task.state_changed"
RUN_STARTED = "run.started"
RUN_OUTPUT = "run.output"
RUN_TOOL_USE = "run.tool_use"
RUN_USAGE = "run.usage"
RUN_FINISHED = "run.finished"
AGENT_ACTIVITY_CHANGED = "agent.activity_changed"
ROUTER_DECISION = "router.decision"
CHECKPOINT_CREATED = "checkpoint.created"
VERIFICATION_RESULT = "verification.result"
APPROVAL_REQUESTED = "approval.requested"
APPROVAL_RESOLVED = "approval.resolved"
LEADER_CHANGED = "leader.changed"
ADVISOR_CONSULTED = "advisor.consulted"
DECISION_RECORDED = "decision.recorded"
USAGE_UPDATED = "usage.updated"
PROVIDER_STATUS = "provider.status"
SKILL_STATE_CHANGED = "skill.state_changed"
KNOWLEDGE_INDEXED = "knowledge.indexed"
SYSTEM_RECOVERY = "system.recovery"
SYSTEM_NOTICE = "system.notice"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TASK_CREATED,
        TASK_UPDATED,
        TASK_STATE_CHANGED,
        RUN_STARTED,
        RUN_OUTPUT,
        RUN_TOOL_USE,
        RUN_USAGE,
        RUN_FINISHED,
        AGENT_ACTIVITY_CHANGED,
        ROUTER_DECISION,
        CHECKPOINT_CREATED,
        VERIFICATION_RESULT,
        APPROVAL_REQUESTED,
        APPROVAL_RESOLVED,
        LEADER_CHANGED,
        ADVISOR_CONSULTED,
        DECISION_RECORDED,
        USAGE_UPDATED,
        PROVIDER_STATUS,
        SKILL_STATE_CHANGED,
        KNOWLEDGE_INDEXED,
        SYSTEM_RECOVERY,
        SYSTEM_NOTICE,
    }
)


class Event(BaseModel):
    seq: int
    ts: str
    type: str
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
