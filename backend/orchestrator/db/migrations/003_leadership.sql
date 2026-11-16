-- M3: leader / advisor intelligence.
-- runs.task_id and checkpoints.task_id become nullable: objective decomposition, leader-switch validation
-- and task-less advisor consults are recorded runs that belong to no task; a `leadership` checkpoint spans
-- all tasks. SQLite cannot drop NOT NULL, so both tables are rebuilt (nothing references them by FK).
-- decisions gains kind / evidence_status / meta (ADR-style records written by advisor + leader).
-- leadership: the row can never be deleted once created (exactly-one-leader invariant, see schema.sql CHECK id = 1).

CREATE TABLE runs_new (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    phase           TEXT NOT NULL CHECK (phase IN ('plan', 'execute', 'repair', 'review', 'advise')),
    agent_id        TEXT,
    provider        TEXT NOT NULL,
    model           TEXT,
    session_id      TEXT,
    pid             INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('starting', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
    error_category  TEXT,
    error_message   TEXT,
    exit_code       INTEGER,
    usage           TEXT NOT NULL DEFAULT '{}',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    heartbeat_at    TEXT,
    idempotency_key TEXT NOT NULL UNIQUE
);
INSERT INTO runs_new SELECT id, task_id, phase, agent_id, provider, model, session_id, pid, status, error_category,
    error_message, exit_code, usage, started_at, finished_at, heartbeat_at, idempotency_key FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX idx_runs_task ON runs(task_id, started_at);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_phase ON runs(phase, started_at);

CREATE TABLE checkpoints_new (
    id         TEXT PRIMARY KEY,
    task_id    TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    run_id     TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('auto', 'handoff', 'leadership', 'manual')),
    summary    TEXT NOT NULL,
    git_ref    TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO checkpoints_new SELECT id, task_id, run_id, kind, summary, git_ref, created_at FROM checkpoints;
DROP TABLE checkpoints;
ALTER TABLE checkpoints_new RENAME TO checkpoints;
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at);

ALTER TABLE decisions ADD COLUMN kind TEXT NOT NULL DEFAULT 'leader_decision';
ALTER TABLE decisions ADD COLUMN evidence_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE decisions ADD COLUMN meta TEXT NOT NULL DEFAULT '{}';
CREATE INDEX idx_decisions_task ON decisions(task_id, created_at);
CREATE INDEX idx_decisions_project ON decisions(project_id, created_at);

CREATE TRIGGER leadership_no_delete BEFORE DELETE ON leadership
BEGIN
    SELECT RAISE(ABORT, 'the leadership row cannot be deleted (exactly one leader)');
END;
