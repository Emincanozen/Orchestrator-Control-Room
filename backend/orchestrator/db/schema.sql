-- Schema v1. See docs/ARCHITECTURE.md §3. Applied once when PRAGMA user_version = 0.
-- Ids are prefixed ULID-like strings; timestamps are UTC ISO-8601 strings;
-- *_json / documented JSON columns hold Pydantic-validated JSON text.

CREATE TABLE projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    default_branch  TEXT,
    verify_commands TEXT NOT NULL DEFAULT '[]',
    skill_profile   TEXT NOT NULL DEFAULT '{}',
    is_git          INTEGER NOT NULL DEFAULT 0,
    isolation       TEXT NOT NULL DEFAULT 'worktree' CHECK (isolation IN ('worktree', 'in_place')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE objectives (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text                TEXT NOT NULL,
    deliverables        TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    prerequisites       TEXT NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'draft',
    plan                TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX idx_objectives_project ON objectives(project_id);

CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    objective_id         TEXT REFERENCES objectives(id) ON DELETE SET NULL,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    task_type            TEXT NOT NULL DEFAULT 'coding',
    priority             INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    state                TEXT NOT NULL,
    state_reason         TEXT,
    acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
    verify_commands      TEXT NOT NULL DEFAULT '[]',
    plan                 TEXT,
    completed_steps      TEXT NOT NULL DEFAULT '[]',
    pending_steps        TEXT NOT NULL DEFAULT '[]',
    next_action          TEXT,
    relevant_files       TEXT NOT NULL DEFAULT '[]',
    role                 TEXT,
    preferred_provider   TEXT,
    preferred_model      TEXT,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    retry_count          INTEGER NOT NULL DEFAULT 0,
    repair_cycles        INTEGER NOT NULL DEFAULT 0,
    not_before           TEXT,
    worktree_path        TEXT,
    branch               TEXT,
    base_commit          TEXT,
    head_commit          TEXT,
    active_seconds       REAL NOT NULL DEFAULT 0,
    last_activity_at     TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    started_at           TEXT,
    finished_at          TEXT
);
CREATE INDEX idx_tasks_queue ON tasks(state, priority DESC, created_at);
CREATE INDEX idx_tasks_project ON tasks(project_id, state);

CREATE TABLE task_dependencies (
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);
CREATE INDEX idx_deps_reverse ON task_dependencies(depends_on_task_id);

CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
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
CREATE INDEX idx_runs_task ON runs(task_id, started_at);
CREATE INDEX idx_runs_status ON runs(status);

CREATE TABLE events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    type       TEXT NOT NULL,
    project_id TEXT,
    task_id    TEXT,
    run_id     TEXT,
    agent_id   TEXT,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_task ON events(task_id, seq);
CREATE INDEX idx_events_type ON events(type, seq);

CREATE TABLE state_transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT,
    actor      TEXT NOT NULL,
    ts         TEXT NOT NULL
);
CREATE INDEX idx_transitions_task ON state_transitions(task_id, id);

CREATE TABLE checkpoints (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id     TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('auto', 'handoff', 'leadership', 'manual')),
    summary    TEXT NOT NULL,
    git_ref    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, created_at);

-- Deterministic verification records (ADR-007). COMPLETED requires a 'passed'
-- or 'unverifiable_accepted' row for the task.
CREATE TABLE verifications (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id      TEXT,
    status      TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error', 'no_checks', 'unverifiable_accepted')),
    checks      TEXT NOT NULL DEFAULT '[]',
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_verifications_task ON verifications(task_id, started_at);

CREATE TABLE agents (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    role              TEXT NOT NULL,
    room              TEXT NOT NULL,
    provider          TEXT,
    model             TEXT,
    activity          TEXT NOT NULL DEFAULT 'idle',
    current_task_id   TEXT,
    current_run_id    TEXT,
    current_operation TEXT,
    since             TEXT NOT NULL,
    seat_order        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    details      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied')),
    requested_at TEXT NOT NULL,
    resolved_at  TEXT,
    resolved_by  TEXT,
    note         TEXT
);
CREATE INDEX idx_approvals_status ON approvals(status, requested_at);

CREATE TABLE decisions (
    id             TEXT PRIMARY KEY,
    project_id     TEXT,
    task_id        TEXT,
    title          TEXT NOT NULL,
    context        TEXT NOT NULL DEFAULT '',
    options        TEXT NOT NULL DEFAULT '[]',
    decision       TEXT NOT NULL DEFAULT '',
    rationale      TEXT NOT NULL DEFAULT '',
    evidence       TEXT NOT NULL DEFAULT '[]',
    advisor_run_id TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE routing_decisions (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    run_id           TEXT,
    phase            TEXT NOT NULL,
    chosen_provider  TEXT NOT NULL,
    chosen_model     TEXT,
    candidates       TEXT NOT NULL DEFAULT '[]',
    reasons          TEXT NOT NULL DEFAULT '[]',
    rule_ids         TEXT NOT NULL DEFAULT '[]',
    adaptive_applied INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_routing_task ON routing_decisions(task_id, created_at);

CREATE TABLE provider_status (
    provider      TEXT PRIMARY KEY,
    installed     INTEGER NOT NULL,
    version       TEXT,
    authenticated INTEGER,
    executable    TEXT,
    capabilities  TEXT NOT NULL DEFAULT '{}',
    checked_at    TEXT NOT NULL,
    error         TEXT
);

CREATE TABLE provider_models (
    provider     TEXT NOT NULL,
    model_id     TEXT NOT NULL,
    display_name TEXT,
    source       TEXT NOT NULL CHECK (source IN ('cli', 'config', 'static')),
    capabilities TEXT NOT NULL DEFAULT '[]',
    tier         TEXT,
    verified_at  TEXT,
    PRIMARY KEY (provider, model_id)
);

CREATE TABLE usage_snapshots (
    id          TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT,
    state       TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    exactness   TEXT NOT NULL CHECK (exactness IN ('exact', 'estimated', 'unknown')),
    source      TEXT NOT NULL,
    confidence  REAL,
    resets_at   TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX idx_usage_provider ON usage_snapshots(provider, observed_at);

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Exactly-one-leader invariant (ARCHITECTURE §10): at most one row, id = 1.
CREATE TABLE leadership (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    provider   TEXT NOT NULL,
    model      TEXT,
    advisor    TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE knowledge_docs (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    project_id  TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('requirement', 'adr', 'doc', 'task', 'error_resolution', 'code_meta', 'skill', 'model_history')),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    source_path TEXT,
    source_hash TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_knowledge_project ON knowledge_docs(project_id, kind);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    title, body, content='knowledge_docs', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge_docs BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO knowledge_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

CREATE TABLE knowledge_vectors (
    doc_id TEXT NOT NULL REFERENCES knowledge_docs(id) ON DELETE CASCADE,
    chunk  INTEGER NOT NULL,
    vector BLOB NOT NULL,
    model  TEXT NOT NULL,
    PRIMARY KEY (doc_id, chunk, model)
);

CREATE TABLE skills (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('agent_skill', 'mcp_server', 'standalone_app', 'dev_tool', 'collection', 'external_service', 'docs')),
    source_url      TEXT,
    source_verified INTEGER NOT NULL DEFAULT 0,
    uncertainty     TEXT,
    description     TEXT NOT NULL DEFAULT '',
    install_method  TEXT NOT NULL DEFAULT '{}',
    compat          TEXT NOT NULL DEFAULT '{}',
    dependencies    TEXT NOT NULL DEFAULT '[]',
    permissions     TEXT NOT NULL DEFAULT '[]',
    security        TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL,
    pinned_ref      TEXT,
    installed_ref   TEXT,
    previous_ref    TEXT,
    local_path      TEXT,
    project_scopes  TEXT NOT NULL DEFAULT '[]',
    updated_at      TEXT NOT NULL
);

CREATE TABLE skill_events (
    id         TEXT PRIMARY KEY,
    skill_id   TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    ref        TEXT,
    detail     TEXT,
    ts         TEXT NOT NULL
);
CREATE INDEX idx_skill_events_skill ON skill_events(skill_id, ts);
