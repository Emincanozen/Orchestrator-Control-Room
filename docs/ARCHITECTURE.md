# AI Orchestrator — Architecture (V5 target)

This document is the binding contract between modules and between the engineers
(human or AI) who implement them. Code-level contracts live in
`backend/orchestrator/domain/` and `backend/orchestrator/providers/base.py`;
when this document and those files disagree, the code files win and this
document must be fixed.

## 1. Shape

Modular monolith, local-first, single Windows machine, no Docker/Redis/cloud.

```
 Browser (React + PixiJS)  ──HTTP/WS──┐
 orchestrator CLI (Typer)  ──HTTP─────┤
                                      ▼
                    FastAPI app (127.0.0.1 only, token auth)
   ┌───────────┬───────────┬───────────┬───────────┬────────────┐
   │ Scheduler │ Leader /  │ Router +  │ Knowledge │ Skills/MCP │
   │ Supervisor│ Advisor   │ Usage     │ Analytics │ Manager    │
   └─────┬─────┴─────┬─────┴─────┬─────┴─────┬─────┴──────┬─────┘
         ▼           ▼           ▼           ▼            ▼
      Event bus (persist first, then broadcast)  ──►  SQLite (WAL)
         ▼
   Provider adapters ──► subprocess: claude / codex / agy (arg arrays, stdin prompts)
   Git + worktrees   ──► one worktree per running task (outside the data dir)
```

Rules:

1. **Backend owns truth.** SQLite is the only durable state. UI and CLI are
   clients of the same HTTP API. The UI renders recorded events only.
2. **Persist, then publish.** Every state change is written (state row + event
   row in one transaction) before it is broadcast over WebSocket.
3. **Deterministic first.** Scheduling, state transitions, verification,
   routing rules, recovery and fallback are plain code. LLM calls happen only
   for planning, execution, review and advice.
4. **No LLM call is ever caused by rendering, polling or navigation.**

## 2. Repository layout

```
backend/
  pyproject.toml              # uv-managed, Python 3.12
  orchestrator/
    config.py                 # paths, YAML loading, settings
    domain/                   # states.py, models.py, events.py  (CONTRACT)
    db/                       # schema.sql, database.py (stdlib sqlite3, WAL, one writer)
    events/bus.py             # append + broadcast, replay by seq
    providers/                # base.py (CONTRACT), process.py, claude.py, codex.py,
                              # antigravity.py, mock.py, registry.py
    router/                   # classifier.py, router.py, capabilities.py
    usage/monitor.py
    scheduler/                # queue.py, scheduler.py, supervisor.py, executor.py, verification.py
    checkpoints/              # manager.py, handoff.py
    gitops/                   # git.py, worktrees.py
    leader/                   # controller.py, advisor.py, decisions.py
    knowledge/                # index.py (FTS5), retrieval.py, vectors.py (optional)
    analytics/                # metrics.py, adaptive.py
    skills/                   # catalog.py, manager.py
    security/                 # auth.py, paths.py, approvals.py, redact.py, acl.py, worker_job.py,
                              # peer.py, instance_lock.py, sandbox.py, agent_files.py, python_runtime.py
    api/                      # app.py, ws.py, routes/*.py
    cli/main.py               # `orchestrator` entry point
  tests/                      # unit/, integration/, contract/, recovery/, security/, smoke/ (real CLIs, opt-in)
frontend/                     # Vite + React + TS + Tailwind + PixiJS
  src/api/ src/store/ src/office/ src/views/ src/components/
  e2e/                        # Playwright
config/                       # models.yaml routing.yaml policies.yaml skills-catalog.yaml
docs/
scripts/                      # start.cmd, stop.cmd, orchestrator.cmd launcher

Runtime data is outside the repository (the old `data/` folder is unused, still git-ignored):
  %LOCALAPPDATA%\AI-Orchestrator\            # data dir (ORCHESTRATOR_DATA_DIR): orchestrator.db, api-token,
                                            # logs\, skills\, server.pid, server.lock
  %LOCALAPPDATA%\AI-Orchestrator-worktrees\  # worktrees dir (ORCHESTRATOR_WORKTREES_DIR, default <data dir>-worktrees):
                                            # <task_id>\ per task, _scratch\leader\ for leader/advisor runs
(non-Windows: $XDG_DATA_HOME or ~/.local/share, + /ai-orchestrator). The two dirs must not contain each other.
```

## 3. Domain model (SQLite tables)

All ids are ULID-like strings with a type prefix (`prj_`, `tsk_`, `run_`, ...).
All timestamps are UTC ISO-8601 strings. JSON columns hold Pydantic-validated data.

| Table | Purpose | Key columns |
|---|---|---|
| `projects` | Registered workspaces | id, name, path (unique, validated), default_branch, verify_commands(json), skill_profile(json), created_at |
| `objectives` | A user goal decomposed by the leader | id, project_id, text, deliverables(json), acceptance_criteria(json), prerequisites(json), status, plan(json) |
| `tasks` | Unit of schedulable work | id, project_id, objective_id?, title, description, task_type, priority(0-100), state, state_reason, acceptance_criteria(json), verify_commands(json), plan(json), completed_steps(json), pending_steps(json), next_action, relevant_files(json), role, preferred_provider?, preferred_model?, attempt_count, consecutive_failures, worktree_path?, branch?, base_commit?, head_commit?, active_seconds, last_activity_at, created_at, updated_at, started_at?, finished_at? |
| `task_dependencies` | DAG edges | task_id, depends_on_task_id |
| `runs` | One provider execution attempt of a task phase | id, task_id, phase(plan/execute/repair/review/advise), agent_id, provider, model, session_id?, pid?, status(starting/running/succeeded/failed/cancelled/interrupted), error_category?, error_message?, exit_code?, usage(json), started_at, finished_at?, heartbeat_at, idempotency_key (unique) |
| `events` | Append-only history, `seq` INTEGER PK AUTOINCREMENT | seq, ts, type, project_id?, task_id?, run_id?, agent_id?, payload(json) |
| `state_transitions` | Audit of task state changes | id, task_id, from_state, to_state, reason, actor, ts |
| `checkpoints` | Structured resume points | id, task_id, run_id?, kind(auto/handoff/leadership/manual), summary(json: the Handoff model), git_ref?, created_at |
| `agents` | Office seats (workers) | id, name, role, room, provider?, model?, activity, current_task_id?, current_run_id?, current_operation?, since |
| `approvals` | Human gates | id, task_id?, kind, summary, details(json), status(pending/approved/denied), requested_at, resolved_at?, resolved_by? |
| `decisions` | ADR-style decision records | id, project_id?, task_id?, title, context, options(json), decision, rationale, evidence(json), advisor_run_id?, created_at |
| `routing_decisions` | Every router choice | id, task_id, run_id?, phase, chosen_provider, chosen_model, candidates(json with scores), reasons(json), rule_ids(json), adaptive_applied(bool), created_at |
| `provider_status` | Latest detection per provider | provider, installed, version, authenticated, executable, capabilities(json), checked_at, error? |
| `provider_models` | Discovered model registry | provider, model_id, display_name, source(cli/config/static), capabilities(json), tier, verified_at |
| `usage_snapshots` | Quota/usage observations | id, provider, model?, state, metric, value?, unit?, exactness(exact/estimated/unknown), source, confidence, resets_at?, observed_at |
| `settings` | Key/value JSON (leadership, limits, policies overrides) | key, value(json), updated_at |
| `knowledge_docs` + `knowledge_fts` (FTS5) | RAG corpus with provenance | id, project_id?, kind(requirement/adr/doc/task/error_resolution/code_meta/skill/model_history), title, body, source_path?, source_hash, updated_at |
| `knowledge_vectors` | Optional selective embeddings | doc_id, chunk, vector(blob), model |
| `skills` | Catalog entries | id, name, category, kind(agent_skill/mcp_server/standalone_app/dev_tool/collection/external_service/docs), source_url, source_verified(bool), uncertainty?, description, install_method(json), compat(json: claude_code/codex/mcp), dependencies(json), permissions(json), security(json), state, pinned_ref?, installed_ref?, previous_ref?, local_path?, project_scopes(json), updated_at |
| `skill_events` | Install/activate/rollback audit | id, skill_id, action, from_state, to_state, ref?, detail, ts |

Memory separation (PART 16): operational state = `tasks/runs/checkpoints`;
short-term task context = handoff built per run; project knowledge + historical
decisions = `knowledge_docs`/`decisions`; model performance = `runs` +
`routing_decisions` aggregated by analytics; skill knowledge = `skills`.

## 4. Task state machine

Defined in `domain/states.py` (`TaskState`, `ALLOWED_TRANSITIONS`,
`assert_transition`). Terminal: `COMPLETED`, `CANCELLED`. `FAILED` may be
re-queued only by an explicit retry. Every transition writes
`state_transitions` + a `task.state_changed` event atomically. Illegal
transitions raise `InvalidTransition` — never silently coerced.

Pipeline for one task:

```
CREATED → QUEUED → PLANNING → RUNNING → TESTING → REVIEWING → COMPLETED
                      │          │  ▲       │          │
                      │          │  └─repair┴──────────┘   (bounded by policies)
                      ▼          ▼
        WAITING_FOR_QUOTA / NEEDS_APPROVAL / PAUSED / BLOCKED / FAILED / CANCELLED
```

- PLANNING may be skipped for tasks that already carry a plan + criteria.
- TESTING runs deterministic verification (`verify_commands`, file checks).
  **COMPLETED requires a passing verification record**; a model's claim is not evidence.
- A task with no verification possible ends as COMPLETED only with
  `verification.status = "unverifiable_accepted"` after explicit user approval;
  otherwise it stops in NEEDS_APPROVAL.

## 5. Event schema

`{seq, ts, type, project_id?, task_id?, run_id?, agent_id?, payload}`

Types: `task.created` `task.updated` `task.state_changed` `run.started`
`run.output` `run.tool_use` `run.usage` `run.finished` `agent.activity_changed`
`router.decision` `checkpoint.created` `verification.result`
`approval.requested` `approval.resolved` `leader.changed` `advisor.consulted`
`decision.recorded` `usage.updated` `provider.status` `skill.state_changed`
`knowledge.indexed` `system.recovery` `system.notice`.

WebSocket `GET /ws?after_seq=N`: the token is sent as the subprotocol `orch.token.<token>`
(`?token=` is still accepted for compatibility). The server replays events with
`seq > N`, then streams live. Clients reconnect with their last seen seq, so
the UI is always a pure function of the event log + REST snapshots.

Replay is capped at `limits.ws_replay_max_events` (2,000). When more events are pending the
server sends `{"type":"replay.truncated","requested_after_seq","resume_after_seq","skipped","cap","seq"}`
before the replayed events (replay then starts at the newest events, after `resume_after_seq`; `seq` in a
control frame is not an event seq), and `replay.done` carries `truncated: true`. Clients reconcile the
skipped gap over REST.

## 6. HTTP API (prefix `/api`, JSON, bearer token)

| Area | Endpoints |
|---|---|
| System | `GET /health` (no auth) · `GET /status` · `GET/PUT /settings` |
| Providers | `GET /providers` · `POST /providers/refresh` · `GET /models` · `GET /usage` |
| Projects | `GET/POST /projects` · `GET/PATCH/DELETE /projects/{id}` |
| Objectives | `POST /projects/{id}/objectives` · `GET /objectives/{id}` |
| Tasks | `GET /tasks?project_id=&state=` · `POST /tasks` · `GET/PATCH /tasks/{id}` · `POST /tasks/{id}/{start,pause,resume,cancel,retry}` · `GET /tasks/{id}/{events,runs,checkpoints,diff,artifacts,verification}` |
| Agents | `GET /agents` · `GET /agents/{id}` (inspector payload) |
| Events | `GET /events?after_seq=&limit=&task_id=` · `WS /ws` |
| Approvals | `GET /approvals?status=` · `POST /approvals/{id}/resolve {approve: bool, note}` |
| Leadership | `GET/PUT /leadership` · `POST /leadership/switch` · `POST /advisor/consult` · `GET /decisions` |
| Routing | `GET /routing/decisions?task_id=&limit=` · `POST /routing/preview` · `GET/PUT /routing/preferences` |
| Knowledge | `POST /knowledge/search` · `POST /knowledge/index` · `GET /knowledge/docs` |
| Analytics | `GET /analytics/summary` · `GET /analytics/models` · `GET /analytics/routing` |
| Skills | `GET /skills?q=&category=&state=` · `GET /skills/{id}` · `POST /skills/{id}/{install,activate,deactivate,update,rollback}` |

Errors: `{"error": {"code": "...", "message": "...", "details": {...}}}` with
proper HTTP status. Request-guard codes returned before authentication: `worker_origin_refused` (403, the request
comes from a process in the worker job) and `peer_unresolved` (403, loopback peer not identifiable, state-changing
methods and WS only); the WebSocket close code for both is 4403 (4401 = bad token, 4408 = slow client).
`/docs`, `/redoc` and `/openapi.json` exist only with `ORCHESTRATOR_DEV=1`. Every measured number in API responses that can be
estimated or missing is an object `{value, exactness, source}` — never a bare
invented number.

## 7. Provider adapters

Contract: `providers/base.py::ProviderAdapter`. Verified facts (2026-11-11,
this machine) are in `config/models.yaml`.

| | Claude Code 2.1.322 | Codex CLI 0.167.1 | Antigravity `agy` 1.5.5 |
|---|---|---|---|
| Non-interactive | `claude -p` | `codex exec` | `agy -p` |
| Structured stream | `--output-format stream-json --verbose` | `--json` (JSONL) | `--output-format stream-json` |
| Session id | `system/init.session_id` | `thread.started.thread_id` | `init.conversation_id` |
| Resume | `--resume <id>` | `codex exec resume <id>` | `--conversation <id>` |
| Model flag | `--model` | `-m` | `--model` |
| Model discovery | aliases + init event | config default only (no list command found) | `agy models` |
| Usage | per-result tokens, `total_cost_usd`, `rate_limit_event` (window utilization, resetsAt) | `turn.completed.usage` tokens | per-step + result tokens |
| Sandbox/permissions | `--permission-mode`, `--tools`, `--allowedTools` | `-s read-only/workspace-write` | `--mode plan/accept-edits/auto`, `--sandbox` |
| Working dir | cwd | `-C` | cwd / `--add-dir` |

Execution rules:

- Arg arrays only; **prompts go through stdin** (or a temp file), never
  interpolated into a shell string. `.cmd` npm shims are resolved to their real
  executable/JS entry so `cmd.exe` argument parsing is never involved.
- Each run is a child process in its own process group; cancel = terminate the
  whole tree (`taskkill /T /F` fallback), then mark run `cancelled`. On Windows every agent,
  helper and verification child is created suspended, assigned to a per-server Job Object
  (no breakaway, kill-on-close) and resumed (`security/worker_job.py`).
- Recursion guard: adapters set `AI_ORCHESTRATOR_DEPTH` in the child env and
  refuse to start when it is already ≥ 1. Child environments never contain `ORCHESTRATOR_*`
  variables. A run whose cwd or extra dir is, is inside, or contains the data dir is refused.
  Claude runs always use `--restricted --strict-mcp-config --disable-slash-commands`,
  an explicit file-tool allowlist (no shell, no web by default; Bash only with the YAML-only
  policy `worker_permissions.claude_shell: true`) and `--settings` deny rules for the data dir.
  Codex runs add `--ignore-rules --disable hooks` to the existing plugin/MCP disabling.
  Antigravity adds `--disable-slash-commands`. Per-provider confinement status is in
  [SECURITY.md](SECURITY.md); the contract does not promise uniform confinement.
- Unsupported operations raise `UnsupportedOperation` and are reported in
  `capabilities` — never faked.
- Error classification → `ErrorCategory` (rate_limit, quota_exhausted, auth,
  network, timeout, invalid_request, tool_failure, cancelled, crashed, unknown).
  Only `rate_limit`/`quota_exhausted` lead to WAITING_FOR_QUOTA.

## 8. Scheduler, supervisor, executor

- **Queue**: durable — it is simply `tasks WHERE state='QUEUED'` ordered by
  priority desc, created_at. No in-memory-only queue.
- **Scheduler loop** (asyncio): picks runnable tasks = QUEUED, all dependencies
  COMPLETED, project/global concurrency slot free, no other active task on the
  same worktree. Dependency FAILED/CANCELLED → dependent goes BLOCKED with reason.
- **Executor** drives one task through the pipeline, one `run` per phase attempt.
  Each run has a unique `idempotency_key = task_id:phase:attempt`.
- **Supervisor** (deterministic): heartbeats, max runtime, retry/consecutive
  failure limits, repair loop bounds, quota waits with verified resume, approval
  gates. Limits come from `config/policies.yaml` (overridable via settings).
- **Isolation**: every executing task gets `<worktrees dir>/<task_id>` (a sibling of the data
  dir, not inside it) on branch `orch/<task_id>` from the project's base commit. Every
  orchestrator git call uses a hardening prefix (hooks, fsmonitor, filters, pager, editor,
  credential helper off; `--no-verify`; allowlisted environment; `gitops/git.py`). Non-git projects are either
  registered for explicit Git-initialisation approval; execution remains BLOCKED until approval creates
  the repository and a task-specific worktree can be established. Legacy `in_place` rows never permit
  provider execution in the source checkout. An already-Git legacy row is upgraded only after validating
  its actual repository root and successfully creating/reusing the owned task worktree; no Git reinitialisation
  is performed. For non-Git rows, approval and subsequent retry remain explicit actions.
- **Recovery on startup**: runs in `starting/running` whose pid is dead →
  `interrupted`; their tasks → QUEUED with a recovery checkpoint (if resumable)
  or BLOCKED (if side effects are uncertain). A task found in TESTING is always
  security-BLOCKED for interrupted verification, even if its provider run already
  succeeded or its PID is absent; no automatic handoff checkpoint is made from
  that unverified payload. The persisted BLOCKED state survives later restarts.
  Human review is required before an explicit retry. This does not prove native
  process exit or implement a complete persistent quarantine. Emits `system.recovery`.
  A stored PID and timestamp window never authorise process termination. A live/unreadable recorded PID
  instead persists an uncertainty marker on the interrupted run, blocks provider execution before worktree
  preparation, and quarantines only the exact task path verified by directory guards and trusted Git
  registration. Source/other-task paths are never quarantined from DB pointers. Restart reapplies unresolved
  barriers; confirmed process absence atomically retires the marker so later PID reuse is unrelated history.
  Same-server quarantine is not cleared: after absence, restart and explicitly retry. Normal Windows crash
  cleanup belongs to the existing KILL_ON_CLOSE worker job, never PID guessing at startup.

## 9. Router and usage

`router.route(task, phase) -> RoutingDecision`:

1. Classify (deterministic keywords/metadata → task_type, complexity, risk,
   required capabilities). Visual capabilities are distinct flags:
   `image_understanding, image_generation, image_editing, ui_implementation,
   visual_design, browser_inspection, screenshot_analysis, pixel_art, animation,
   video_generation`.
2. Filter candidates: installed ∧ authenticated ∧ has required capabilities ∧
   usage state not EXHAUSTED/AUTH_ERROR/COOLDOWN ∧ policy allows.
3. Score by rules in `config/routing.yaml` (role tier fit, user preference,
   context continuity, usage headroom, cost class).
4. Adaptive adjustment only when a (provider, model, task_type) cell has
   ≥ `min_samples` verified runs; adjustment is bounded and recorded.
5. Persist decision with candidates, scores and human-readable reasons.

Fallback uses the same router with the failed candidate excluded and a handoff
checkpoint; if no *suitable* candidate remains the task waits or blocks — it is
never sent to an unsuitable model.

Usage states: AVAILABLE, LOW, EXHAUSTED, COOLDOWN, UNKNOWN, AUTH_ERROR,
PROVIDER_ERROR. Sources: Claude `rate_limit_event` (exact window utilization),
token counts from run results (exact per run), classified errors (exact state,
unknown amount). Anything else is UNKNOWN. Billing classes: `subscription_cli`,
`metered_api`, `local`, `external_paid`; only `subscription_cli` and `local`
are enabled without explicit approval.

## 10. Leader and advisor

- Exactly one leader: `settings['leadership'].leader = {provider, model}` plus
  a DB-level single row; leader runs are `phase in (plan, review)`.
- Leader duties are *bounded calls* with JSON-schema outputs (plan,
  decomposition, review verdict). The deterministic supervisor decides when to
  call it.
- Switch protocol (`POST /leadership/switch`): pause assignment → leadership
  checkpoint (active/pending tasks, open decisions) → set new leader →
  validation call (new leader must restate state as schema-valid JSON matching
  the checkpoint's task ids) → resume. Healthy workers keep running.
- Advisor: optional `{provider, model, policy: never|on_request|auto, max_consults}`.
  Receives problem, requirements, evidence, candidate options, constraints —
  not the leader's transcript. Result is stored as a `decisions` row; the
  leader's final call must cite evidence (tests/diffs), not agreement.

## 11. Knowledge, analytics, skills

- Knowledge: SQLite FTS5 over `knowledge_docs` with provenance + content hash
  for incremental indexing. Exact facts come from SQL. Vectors are optional and
  selective (off by default; never required).
- Analytics: computed from `runs`, `tasks`, `state_transitions`,
  `routing_decisions`. Every metric carries exactness (measured/estimated/
  ai_evaluated/missing).
- Skills & MCP Manager: catalog from `config/skills-catalog.yaml` → `skills`
  table. States: `CATALOGUED_ON_DEMAND`, `DOWNLOADED_AND_DISABLED`,
  `INSTALLED_AND_ACTIVE`, plus `INSTALL_FAILED`, `INCOMPATIBLE`,
  `NEEDS_SECURITY_REVIEW`. Install = fetch pinned commit into `<data dir>/skills/`
  (no script execution) → review gate → activate by copying into the project's
  `.claude/skills` / registering MCP per project. Rollback restores
  `previous_ref`. Nothing third-party executes without an approval record.

## 12. Security

- Bind `127.0.0.1` only; validate `Host` and `Origin`; bearer token from `api-token` in the data
  dir (random, created on first start) on every route except `/health`; WS requires the token
  (subprotocol `orch.token.<token>`; `?token=` accepted for compatibility). The dashboard is
  opened with `/#token=<token>` (URL fragment, never sent to the server).
- Data dir, worktrees dir and token file get an owner-only ACL via `icacls` (`security/acl.py`;
  `ORCHESTRATOR_HARDEN_ACL=0` disables it). It excludes other local accounts, not same-user processes.
- Worker-origin request guard (Windows): the API refuses HTTP and WS requests whose local TCP
  peer is a process in the per-server worker Job Object (403 `worker_origin_refused`, WS close 4403)
  before authentication; an unresolvable loopback peer is refused for state-changing methods and
  WS (403 `peer_unresolved`); `/api/health` stays open. Limits: brokered processes (Task
  Scheduler, WMI, COM/ShellExecute, services) are outside the job; 1-second verdict cache (GET/HEAD only) per
  client endpoint; POSIX is best-effort (PID ancestry) (`security/peer.py`, `api/security.py`).
- Single-instance lock `server.lock` (OS-level, `security/instance_lock.py`): a second server on
  the same data dir fails before recovery.
- Path policy: agent cwd and all file-serving endpoints must resolve inside a
  registered project path or its worktree. Project registration refuses drive roots, the user
  profile and its parent, `%APPDATA%`/`%LOCALAPPDATA%`/`%ProgramData%` and anything containing
  them, `%SystemRoot%`, Program Files, and anything overlapping the data or worktrees dir.
- Redaction filter on all logged/streamed text (API keys, tokens, bearer headers).
- Child env is an allowlist with no `ORCHESTRATOR_*`; secrets are not forwarded unless the provider needs them.
- Session ids are validated (`[A-Za-z0-9._:-]`, 1-128, no leading `-`) and passed as `--resume=<id>` / `--conversation=<id>`.
- Skills: reinstall/removal confines the marker's file list to the skill's install dir; activation
  approvals show the exact MCP command, args and env key names and are bound to a content hash
  re-verified at activation.
- Events: retention (`limits.events_max_age_days`, `limits.events_max_rows`; events of unfinished
  tasks kept); WS replay capped (`limits.ws_replay_max_events`, see §5).
- Dev-only surface: `/docs`, `/redoc`, `/openapi.json` and the 5173 dev origins exist only with `ORCHESTRATOR_DEV=1`.
- Approval gates: destructive git ops, deletes outside worktree, deploys,
  paid/metered activation, untrusted installs, privilege escalation (`permission_escalation`
  text is provider-specific).
- External content (repo files, web, tool output, skills) is data, never instructions.
- Verification commands require a per-task, zero-capability Windows AppContainer plus a reduced Low-integrity
  token, worker job and per-command job/desktop. Validate the suspended child's exact AppContainer SID,
  capability count and absence of a loopback exemption before resume. Missing controls or modified configuration
  block the task. In-place command verification is refused. Git and verification
  use atomic worktree reservations, including raw Git plumbing and worktree removal. Ancestor directory handles
  deny WRITE/DELETE sharing during file access, creation, cleanup and configuration restore on Windows.
- Copy-in verification (`security/verify_tree.py`): a verification command never runs in the task worktree and
  its AppContainer identity is never granted there. Per command the orchestrator copies the worktree through
  no-follow, handle-checked reads (reparse points and multi-link files refused, unnamed data stream only) into
  `<worktrees>/_sandbox/<project>/runs/<random>/tree`, granted/labelled/pinned while still empty; the identity
  is derived from that random path (one fresh SID per command). Scratch and tool caches live in the same run
  directory. Copy entries are created exclusively relative to held parent-directory handles (`NtCreateFile`
  with `RootDirectory`, parents held without delete sharing) and stamped through their own handle; nothing is
  created or `utime`d by path. `runs/` gets a protected DACL (user, SYSTEM, Administrators) and a run directory
  or copy whose DACL grants S-1-15-2-1 / S-1-15-2-2 any write right is refused. The run directory is held (no
  delete sharing) for the session and removed after the job is drained, without following links: every entry is
  opened relative to its held parent with `FILE_OPEN_REPARSE_POINT` and deleted through that handle
  (`FileDispositionInfoEx`: POSIX semantics, read-only ignored; links removed as links; no MAX_PATH limit). The
  project root and `runs/` are held for the session; the run directory is created, reopened (by file id) and
  removed relative to them, never by path, within a bounded deadline (`DESTROY_SECONDS`). Scratch is granted
  Modify + DELETE_CHILD (0x1301ff) WITHOUT WRITE_DAC/WRITE_OWNER (the Python 0700 bootstrap creates its
  directory with an explicit descriptor, which needs neither; verified natively). A DACL the command changed
  anyway (objects it created with an explicit descriptor) is reset via the owner's implicit WRITE_DAC, never on
  a name of a multi-link file; resets are logged as one summary line per run. What still cannot be removed is
  contained residue (held, plain, same file id, inside the Medium protected `runs/`, no live process): logged in
  `Session.residue`, never a `sandbox_tamper`, and the run directory is renamed by handle into the protected
  `<project>/residue/`, which the per-command sweep skips; `residue/` is retried at most every 5 minutes per
  project and process with a per-object (file id) exponential backoff (60 s .. 1 h). `verify_tree.sweep` runs
  after every command and once per process over `_sandbox/*` (also removing the legacy per-project `tmp/` and
  `cache-*`): it holds the sandbox dir, project root, `runs/` and `residue/` without delete sharing, enumerates
  through those handles and opens every entry relative to them (nothing removed by path); a project root
  carrying a Low label or a write grant for an S-1-15-* / Everyone / Anonymous SID (or a NULL DACL) is not swept
  (rebuilt by `medium_plain_root` before its next use). A run directory that is not the object created is
  tamper; a concurrent removal by another orchestrator process (sharing violation / delete pending while
  `destroy` reopens it) is residue/retry. Nothing is copied back, so command output files never reach the worktree, a commit or a
  later command (per-command copy semantics; contract change of 6e02cc9). Worktrees carry no sandbox grant and
  no Low label; a worktree whose root still carries an EXPLICIT (non-inherited) Low label or S-1-15-2-* ACE from
  an older version (pinned roots deny the owner WRITE_DAC/WRITE_OWNER, so the marks cannot be stripped in place)
  is, under one Git reservation of the worktree, moved aside into `<worktrees>/_legacy/<task>-<stamp>-<rand>`
  (preserved, its `.git` link renamed so it no longer points at the reused admin dir) and recreated from its
  branch, but only when HEAD is attached to `refs/heads/orch/<task>` at its tip, `status` is clean and no nested
  `.git` exists below the root; otherwise it is refused (`UnsafeWorktree`). Submodule state is detected by the
  nested-`.git` scan rather than `--ignore-submodules=none`, which would make git read submodule configuration. Bounds (200k entries, 2 GiB,
  depth 64) fail closed; the pre-scans and the per-chunk copy loop observe the command deadline. Absolute
  worktree paths in arguments (whole argument or `--opt=<path>`, case-insensitive, either separator) are mapped
  into the copy; output maps the copy back to the worktree and scratch/cache/run paths to placeholders.
- `security/python_runtime.py` routes Python verification through a separately copied interpreter/dependency
  tree, validates all content/ACLs, and retains file and ancestor leases until the command job is drained.
  Source Python/profile/system ACLs are untouched. New task scratch uses an exact task identity; 0700 directories
  are constructed with protected ACLs there and moved atomically without replacement into the task tree. The
  process-wide shared lease re-checks per acquire, on the held pins, DACL/owner/label and attributes, file id
  and link count against the first-acquire snapshot. Lock order `_runtime_lock` -> `refs_lock` (re-entrant: a
  GC-run finalizer may release a reference on a thread already inside it).
- Verification startup carries an absolute budget guard into the native Session. Preparation, runtime
  acquisition and final pre-resume confinement all consume the original command budget; expiry vetoes
  execution and drains any still-suspended child before releasing leases. Trusted native calls remain
  joined rather than force-interrupted. This guard does not diagnose or conceal slow setup.
- Every provider/helper spawn receives its own non-inheritable lifetime job before resume. Parent exit triggers
  descendant termination and ActiveProcesses==0 verification, independently of inherited pipe EOF. Provider
  execution holds a worktree reservation through cleanup. Unknown exit quarantines source/affected worktrees
  against Git, providers and verification until server exit (the outer worker job is the crash boundary).
- N2 remains PARTIAL/OPEN pending the complete security/release gates. Standard AppContainer broker residuals,
  npm runtime compatibility, in-place command verification and unknown crash-left profiles remain documented.

### Configuration and environment

Environment: `ORCHESTRATOR_DATA_DIR`, `ORCHESTRATOR_WORKTREES_DIR` (default `<data dir>-worktrees`),
`ORCHESTRATOR_HARDEN_ACL` (`0` disables ACL hardening), `ORCHESTRATOR_DEV` (`1` enables the
dev-only surface), plus the existing `ORCHESTRATOR_CONFIG_DIR`, `ORCHESTRATOR_TOKEN_FILE`,
`ORCHESTRATOR_URL`, `ORCHESTRATOR_ENABLE_MOCK`, `ORCHESTRATOR_DISABLE_REAL_PROVIDERS`.
Policy keys (`config/policies.yaml`): `worker_permissions.claude_shell` (default `false`; YAML-only,
the API rejects it with 422 and a settings-table override is ignored), `limits.events_max_age_days`
(30), `limits.events_max_rows` (200000), `limits.ws_replay_max_events` (2000).

## 13. Frontend

- State: one event-sourced store fed by REST snapshot + WS stream.
- Views: Office (PixiJS), Mission Control (projects/tasks/detail), Leadership,
  Usage & Providers, Analytics, Knowledge, Skills & MCP, Approvals, Terminal/Logs.
- Pixel Office: original procedurally-drawn pixel assets (no external art
  dependency), rooms mapped to agent roles, activity → animation table from
  PART 6. Agents are positioned from `agents` rows; animation state derives
  only from `agent.activity_changed` events.


Verification native setup/start/cleanup run in joined background threads. Cancellation records a permanent
nonblocking pending-start veto, waits for the in-flight native operation, adopts any late process/job and
drains it before releasing handles, runtime leases or the worktree reservation. The final pre-resume
cancellation check is the resume commit: a cancellation after it is handled by joined late-process cleanup.
Unverified job exit preserves quarantine. The original command deadline includes setup; it is not reset.
Trusted native setup cannot be force-stopped, so cancellation completion can wait for it while the API
remains responsive. Token/SID/integrity/Job Object checks still precede worker resume.


Codex tool-use payloads distinguish `stage=started/completed`, with stable `tool_call_id`, optional native
`status`/integer `exit_code` and a redacted bounded `diagnostic` for failed/declined/nonzero completions.
Deduplicate call counts by ID, not lifecycle event count. Recovered intermediate errors do not override
model-turn completion; deterministic verification remains required for task completion. A resume request
with nonempty extra_dirs is unsupported by the current CLI mapping and is refused before provider spawn.


### Native verification timing diagnostics

`security/timing.py` holds one bounded ContextVar recorder per verification command and propagates it
to joined native threads. Fixed stage names, cumulative seconds and call counts are logged after cleanup;
no task inputs or exception messages are captured. Measurement/logging errors cannot replace resource
returns, native exceptions or cancellation. Runtime locking retains immediate try/finally release. Nested
stages overlap; child_observation ends on observed exit OR timeout/output cap, not exact kernel exit.
This introduces no HTTP/domain/provider/DB contract change and does not relax any verification deadline.


### Owned Codex prototype (WIP, not production activated)

CommandSpec.environment is an optional per-command copied environment; None preserves the existing
sanitized adapter path. codex_auth/codex_runtime are isolated preparation components and are not yet
connected to CodexAdapter execution. Medium Git assert_plain_worktree uses fresh no-follow metadata and
keeps its trusted .git exception by default; provider consumers must opt into include_git=True. Neither
a scan nor a logical permission setting proves runtime confinement. See OWNED_CODEX_RECOVERY.md for
required reservations, native proof, cleanup and pending integration. No domain/HTTP/DB contract changed.


### Codex activation refusal during recovery

CodexAdapter.execute currently refuses production execution before inventory or spawn and emits the
existing _isolation_error result consumed as scheduler BLOCKED. detect reports this restriction and
withholds sandbox_modes. No public request/config/environment activation bypass exists. The private
fake-transport contract fixture override is not production validation. Owned-runtime integration and
live boundary proof remain mandatory before activation; the legacy runner must not be re-enabled.


## Owned Codex invocation lifecycle (2026-11-14; activation remains blocked)

CodexAdapter owns one context per run ID from before its first preparation await through joined cleanup. The context supplies the immutable per-command environment and pins its private configuration/auth. Inventory and provider launches use that context. Effective inventory must be empty. Initial exec and resume select named orch-read/orch-write permissions and native MXC; resume with extra directories remains unsupported. CliAdapterBase.before_spawn is an internal async hook for immediate revalidation. Pending process-creation tasks are registered separately from running ProcessHandles so cancellation reaches suspended-child abort before native resume. Successful RESULT events are withheld until descendant drain and context cleanup. Unknown lifetime retains the context and raises ContainmentError into task quarantine; no automatic stale-auth cleanup or adoption occurs on restart. Legacy build_command argument rendering remains available for pure historical command contracts, but actual Codex execute always takes the owned path after its unconditional activation refusal. Neither fake transport tests nor preflight scans authorize lifting that refusal.
