# Product Requirements

Scope: V1 to V5 as implemented or targeted in this repository. "Implemented" statements are backed by code; the verification evidence for each feature is summarised in [VERIFICATION.md](VERIFICATION.md) and is not repeated here. Criteria in section 12 are not marked as passed in this document; the verification record does that. The mapping from criteria to tests is in [TEST_PLAN.md](TEST_PLAN.md).

## 1. Purpose and users

AI Orchestrator lets one developer run several AI coding agents (Claude Code, Codex, Antigravity) on local git projects from a single dashboard and CLI, with routing between them, bounded autonomy, and completion decided by deterministic verification rather than by a model's claim.

User: a single developer working on their own Windows machine. There are no accounts, no multi-user roles and no remote access.

## 2. Goals and non-goals

Goals

- Run tasks end to end (plan, execute, verify, review) on local projects, with a durable record of every state change.
- Choose the model per task from verified capabilities, explain the choice, and fall back only to a suitable model.
- Keep the user in control: limits on retries, parallelism and runtime; approvals for risky actions.
- Show only what was recorded. Unknown values stay unknown.
- Use the subscription logins of the provider CLIs; do not enable metered APIs.

Non-goals

- Multi-user, hosted or cloud operation; Docker, Redis or any external service ([ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) ADR-01).
- Running while the PC is off or asleep.
- Replacing the provider CLIs, storing their credentials, or bypassing their quotas.
- Autonomous deployment, purchases or spending. Metered billing is off unless the user explicitly approves it.
- Installing or executing third-party skills or MCP servers without an approval record.
- macOS or Linux support (not tested; paths and process handling target Windows).

## 3. Capability levels

Each level lists requirements (R) and the acceptance criteria from section 12 that check them.

### V1 Foundation and visual experience

- R1.1 Adapters for `claude`, `codex` and `agy` that start non-interactive runs, stream structured events, support cancellation, and report unsupported operations instead of faking them. Criteria AI-01, AI-02.
- R1.2 One backend process owning all state in SQLite; UI and CLI are clients of the same HTTP API; WebSocket stream with replay by sequence number. UI-02, UI-05.
- R1.3 Mission Control: projects, tasks, task board, task detail (overview, live log, timeline, runs, checkpoints, diff, verification, artifacts), task actions. UI-03, UI-04, UI-07.
- R1.4 Pixel Office: one seat per agent with animation derived only from recorded activity events; no model call from rendering. UI-06.
- R1.5 `orchestrator` CLI and Windows launcher scripts. DL-01 to DL-04.

### V2 Intelligent routing

- R2.1 Model discovery and a capability registry; tiers and capabilities come from `config/models.yaml` and CLI output, never invented. AI-01.
- R2.2 Deterministic routing (section 8) with every decision stored with candidates, scores and reasons. AI-03, IN-07.
- R2.3 Capability-aware fallback on quota or authentication errors. AI-04.
- R2.4 Usage monitoring with exactness labels (section 9). IN-01, IN-08.
- R2.5 User routing preferences (provider order, disabled providers and models) and resource limits. UI-07.

### V3 Durable execution

- R3.1 Task state machine with 13 states and atomic transition-plus-event writes (section 5). ER-01.
- R3.2 Durable queue (the task table), dependency ordering, global and per-project concurrency limits. ER-04.
- R3.3 One git worktree per executing task. ER-04.
- R3.4 Checkpoints (automatic, handoff, manual) and crash recovery. ER-05, ER-09.
- R3.5 Deterministic verification and a bounded repair loop. ER-02, ER-03.
- R3.6 Pause, resume, cancel (whole process tree), retry. ER-06, ER-08.
- R3.7 Quota waits that resume only when the usage state changes. ER-07.

### V4 Leader and advisor

- R4.1 Exactly one leader at a time; safe switch with a validation call and rollback. AI-05.
- R4.2 Leader calls are bounded and schema-constrained: plan, objective decomposition, review. AI-06, AI-07.
- R4.3 Optional advisor with a policy (`never`, `on_request`, `auto`) and budgets; its answer is stored as a decision record. AI-08.
- R4.4 Leader-proposed verification passes a safety filter or goes to a human approval. ER-18.

### V5 Knowledge, analytics, extensibility

- R5.1 Project knowledge in SQLite FTS5 with provenance and incremental indexing; retrieved text is labelled untrusted. IN-04, IN-05.
- R5.2 Analytics where every figure carries an exactness label; missing data stays missing. IN-02.
- R5.3 Adaptive routing, only with enough verified evidence, bounded and explained. IN-03.
- R5.4 Skills & MCP Manager (section 10). SK-01 to SK-08.

## 4. Leader and advisor model

- The leader is one `{provider, model}` pair in settings, enforced as a single database row. It is invoked by the deterministic supervisor for discrete decisions with JSON-schema outputs (plan, decomposition, review). It is not a long-lived chat.
- Role minimum tier for the leader is `advanced` (`config/routing.yaml`, role `leader` prefers `frontier` with `advanced` as fallback). Metered models are never chosen as the default leader.
- Switching the leader: pause assignment, write a leadership checkpoint, set the new leader, ask it to restate the coordination state as schema-valid JSON, resume. If validation fails the previous leader is restored. Running workers are not interrupted.
- The advisor is a second opinion, never a second leader. It runs read-only (phase `advise`), receives the problem, requirements, evidence, options and constraints, not the leader's transcript, and prefers a different provider than the leader. Policy and per-task and per-day budgets decide whether a consult is allowed; a refused consult is an explicit error.
- A leader or advisor opinion is not evidence. A leader "approve" verdict does not complete a task without a passing verification record.

## 5. Task lifecycle

States (`backend/orchestrator/domain/states.py`): `CREATED`, `QUEUED`, `PLANNING`, `RUNNING`, `TESTING`, `REVIEWING`, `WAITING_FOR_QUOTA`, `NEEDS_APPROVAL`, `PAUSED`, `BLOCKED`, `FAILED`, `COMPLETED`, `CANCELLED`.

- `COMPLETED` and `CANCELLED` are terminal. `FAILED` leaves only by an explicit retry (back to `QUEUED`) or cancel.
- Planning may be skipped when a task already has a plan and criteria.
- `TESTING` runs deterministic verification. Failed verification returns the task to `RUNNING` (repair) until `max_repair_cycles` is reached, then `FAILED`.
- Illegal transitions raise an error; they are never coerced. Every transition writes the task row, a `state_transitions` row and an event in one transaction before it is broadcast.

Completion rule: a task reaches `COMPLETED` only with a stored passing verification record (commands exit 0, file checks pass), or with an explicit user approval of kind `unverifiable_completion`, recorded as verification status `unverifiable_accepted`. A model's statement that it is done is never sufficient ([ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) ADR-04).

## 6. Bounded autonomy

Defaults from `config/policies.yaml`; the user can override the limits from the Settings view (stored in the settings table).

| Limit | Default |
|---|---|
| `max_parallel_workers` | 3 |
| `max_parallel_per_project` | 2 |
| `max_retry_attempts` (per task, across providers) | 3 |
| `max_repair_cycles` (TESTING to RUNNING loops) | 3 |
| `max_consecutive_failures` | 3 |
| `max_active_runtime_seconds` (per run) | 3600 |
| `run_heartbeat_timeout_seconds` | 300 |
| `max_consultations_per_task` | 2 |
| `max_leader_calls_per_task` | 6 |
| `quota_recheck_seconds` | 300 |
| `budgets.external_api_spend_usd` | 0 |
| `budgets.allow_billing_classes` | `subscription_cli`, `local` |

Worker permissions: `workspace_write` for execute and repair phases (confined to the task worktree where the provider's mechanism allows it; see [SECURITY.md](SECURITY.md) for what is verified per provider); `read_only` for plan, review and advise.

Approval kinds listed in `policies.yaml` as required: `destructive_file_delete`, `destructive_git`, `deployment`, `purchase`, `metered_api_activation`, `sensitive_external_action`, `untrusted_tool_install`, `high_privilege_command`, `git_init_in_project`, `merge_to_base_branch`, `unverifiable_completion`. The code that creates approvals today produces these kinds: `unverifiable_completion`, `git_init_in_project`, `permission_escalation`, `high_risk_task`, `untrusted_tool_install`, and the leader gates `leader_verify_commands`, `leader_clarification`, `leader_escalation`, `leader_budget`. The remaining listed kinds are policy entries that no code path creates yet (see [SECURITY.md](SECURITY.md)). `auto_approve` is empty by default.

## 7. Execution model

- Prompts go to the provider CLIs on stdin; processes start from argument arrays; no `cmd.exe` in the execution path of the adapters.
- Each task runs on branch `orch/<task_id>` in `<worktrees dir>/<task_id>` (default `%LOCALAPPDATA%\AI-Orchestrator-worktrees`, outside the data directory). Non-Git projects can be registered for explicit `git init` approval, but tasks remain BLOCKED until an isolated worktree can be established. After approval, retry is explicit; no task executes in the source checkout.
- Each run has an idempotency key `task_id:phase:attempt`. Runs found alive-or-dead after a restart are reconciled by recovery.
- Error categories decide behaviour: only `rate_limit` and `quota_exhausted` lead to `WAITING_FOR_QUOTA`; authentication errors switch provider or block; unknown errors get one retry and are never treated as quota.

## 8. Routing principles

1. Capability-aware: a candidate must be installed, not known-unauthenticated, have the required capabilities, an allowed billing class, a usage state that is not exhausted, and meet the minimum tier.
2. Deterministic first: classification (type, complexity, risk) and scoring are plain code driven by `config/routing.yaml`. Weights: tier fit 40, user preference 25, context continuity 15, usage headroom 15, historical success 5.
3. Adaptive only with evidence: history adjusts a score only for a (provider, model, task type) cell with at least `min_samples` (20) verified runs, bounded by `max_adjustment` (10 points), and the adjustment is recorded and explained.
4. Fallback excludes the failed candidate, never goes below the rule's minimum tier (`allow_lower_tier: false`), is capped at `max_provider_switches_per_task` (3), and when no suitable candidate remains the task waits or blocks instead of using an unsuitable model.
5. Every decision is stored with candidates, scores, reasons and rule ids, and is shown in the dashboard as "why this model".

## 9. Usage honesty

- Each observation is stored with its source, exactness and confidence. Exactness values: `exact`, `estimated`, `unknown` for usage; `measured`, `estimated`, `ai_evaluated`, `missing` for analytics.
- Anything not observed is `UNKNOWN`. No percentage, remaining quota or cost is fabricated. No provider CLI exposes remaining subscription quota, so the orchestrator shows what the CLIs emit: Claude's rate-limit event (window utilisation and reset time), token counts per run, and classified errors.
- Claude's `total_cost_usd` is stored as an estimate (list-price equivalent), not as money charged.
- Billing classes: `subscription_cli`, `metered_api`, `local`, `external_paid`. Only `subscription_cli` and `local` are allowed without an explicit approval.

## 10. Skills & MCP Manager

The catalogue (`config/skills-catalog.yaml`, 41 user-provided entries) is research data. Nothing is installed by importing it.

States: `CATALOGUED_ON_DEMAND`, `DOWNLOADED_AND_DISABLED`, `INSTALLED_AND_ACTIVE`, `INSTALL_FAILED`, `INCOMPATIBLE`, `NEEDS_SECURITY_REVIEW`.

Rules

- Download fetches a pinned commit of an allowed source (https GitHub URLs only) into the data directory's `skills/` folder without `git checkout` and without executing anything.
- Static inspection decides whether a package needs a security review (for example bundled scripts). Such a package is `NEEDS_SECURITY_REVIEW`.
- Activation is per project only (`scope: project`); global configuration is never written. Activation of an untrusted install needs an approved `untrusted_tool_install` approval; without it nothing is written.
- Activation copies skill files into the project's `.claude/skills` or merges an MCP entry into the project's MCP configuration; deactivation removes exactly what was written. A foreign folder or a user-modified MCP entry is never overwritten or removed.
- Update keeps the previous ref; rollback restores it. Update returns the skill to review/disabled and project copies of the old ref are removed.
- Entries without a known source, documentation-only entries and ambiguous names are catalogue-only until the user supplies a source.
- Codex activation is unsupported and refused.

## 11. Security principles

Local only (loopback bind, token, Host/Origin checks); data and worktrees directories outside the repository with an owner-only ACL; no API access for agent and verification processes (worker-origin guard); least privilege per phase, with provider-specific confinement stated plainly; path confinement; secrets redacted and never forwarded to children beyond an allowlist; external content (repository files, web pages, tool output, skills, retrieved knowledge) is data and never instructions; risky actions need a recorded human approval. Controls, code references and open weaknesses are in [SECURITY.md](SECURITY.md).

## 12. Acceptance criteria

Each criterion is one testable sentence. IDs are used by [TEST_PLAN.md](TEST_PLAN.md).

### Interface

- UI-01 Opening the dashboard without a token shows an unauthorised screen and loads no data; a wrong token is reported as rejected.
- UI-02 With a valid token the dashboard removes the token from the address bar and the connection indicator reaches "live".
- UI-03 A user can register a project, create a task, start it and watch it reach COMPLETED in Mission Control without leaving the browser.
- UI-04 Task action buttons (start, pause, resume, cancel, retry) are enabled exactly for the transitions the state machine allows.
- UI-05 After a page reload or a backend restart the dashboard shows the same data as before, resuming the event stream from the last sequence number without duplicates.
- UI-06 The Pixel Office shows one seat per agent, animates only from recorded activity events, and performs no network call or model call by rendering.
- UI-07 Each of the eleven screens (Office, Mission Control, Approvals, Leadership, Providers & Usage, Routing, Analytics, Knowledge, Skills & MCP, Logs, Settings) shows its data or an explicit empty or error state.
- UI-08 Values that are unknown, estimated or missing are labelled as such and are never displayed as zero.

### AI orchestration

- AI-01 Provider detection reports installed, version, authentication (or unknown) and discovered models for each of the three CLIs without making a model call.
- AI-02 Adapters pass prompts on stdin and only fixed or validated flags in the argument array, and never use dangerous permission-skip flags.
- AI-03 The router selects a model from capability, tier, preference, continuity and usage rules and stores the decision with candidates, scores and reasons.
- AI-04 On a quota or authentication error the task moves to another suitable provider with a handoff checkpoint, or waits or blocks when none is suitable, and never to a model below the minimum tier.
- AI-05 Exactly one leader exists at any time, and a leader switch whose validation fails is rolled back to the previous leader.
- AI-06 The leader decomposes an objective into dependency-aware tasks in state CREATED; invalid output gets one repair attempt and then fails with a stated reason.
- AI-07 Leader plan and review calls are read-only, and a leader approval without a passing verification record does not complete the task.
- AI-08 An advisor consult obeys the policy and budgets, receives no leader transcript, and is stored as a decision record that flags missing objective evidence.
- AI-09 A task moved to another provider receives a structured handoff (summary, completed and pending steps, next action, relevant files) instead of a replay of the previous transcript.

### Execution reliability

- ER-01 Every task state change is one of the allowed transitions and is written together with its event before being broadcast.
- ER-02 A task reaches COMPLETED only with a passing verification record or an approved `unverifiable_completion`.
- ER-03 When verification fails the repair loop runs at most `max_repair_cycles` times and the task then ends FAILED, never COMPLETED.
- ER-04 Each executing task runs in its own git worktree and the global and per-project concurrency limits are not exceeded.
- ER-05 After a crash or restart, runs whose process is gone are marked interrupted and their tasks are re-queued with a recovery checkpoint or blocked, without duplicate runs.
- ER-06 Cancelling a task terminates the whole process tree of its run.
- ER-07 A quota or rate-limit error puts the task in WAITING_FOR_QUOTA and it resumes only when the usage state or the router offers a usable candidate; other errors are not treated as quota.
- ER-08 Retry and consecutive-failure limits are enforced, and a FAILED task can be retried only by an explicit action.
- ER-09 A retry resumes the stored provider session when the adapter supports resume.
- ER-10 A pending approval holds the task; approving re-queues it (or completes it for `unverifiable_completion`) and denying blocks it with the reason.
- ER-11 A real provider can complete a task end to end through the full pipeline (worktree, run, verification, completion).
- ER-12 Every API route except `/api/health` rejects a request without the correct bearer token.
- ER-13 Requests with an unexpected Host or Origin are rejected (HTTP 403, WebSocket close 4403).
- ER-14 The server binds to loopback only and the CLI refuses a non-loopback bind without an explicit flag.
- ER-15 Agent working directories and file-serving endpoints reject paths outside registered projects and worktrees, including traversal, UNC, alternate data streams, symlinks and junctions.
- ER-16 Known secret formats and the API token are redacted from stored events and verification output, and the token does not appear in server log lines.
- ER-17 Child processes receive only allowlisted environment variables, and a provider process started from inside an orchestrator-launched agent is refused.
- ER-18 Leader-proposed verification commands containing shell metacharacters, code-execution flags, out-of-worktree paths or non-allowlisted executables are not run and go to a human approval.
- ER-19 Plan, review and advise runs use read-only permission and execute and repair runs use workspace-write inside the worktree, as enforced by each provider's own mechanism (Claude: file tools only, no shell by default; Codex: its workspace-write sandbox; Antigravity: accept-edits).
- ER-20 Codex worker runs do not load the user's own Codex plugins and MCP servers.

### Intelligence

- IN-01 Usage observations carry source and exactness, absent data is UNKNOWN, and cost is labelled an estimate.
- IN-02 Every analytics figure carries an exactness label and an empty database yields missing values, not zeros.
- IN-03 Adaptive routing has no effect below the minimum sample count and above it is bounded and explained in the decision.
- IN-04 Knowledge indexing is incremental, records provenance and content hash, redacts secrets before storage, and writes only to knowledge tables.
- IN-05 Knowledge injected into a prompt is labelled as untrusted reference data and limited by a budget.
- IN-06 Viewing, navigating or refreshing any screen causes no model call.
- IN-07 For every routing decision the dashboard can show the classification, ranked and rejected candidates, scores and reasons.
- IN-08 Only `subscription_cli` and `local` billing classes are eligible for routing and as default leader without explicit approval.

### Skills

- SK-01 Importing the catalogue is idempotent and preserves local state (installed ref, project scopes).
- SK-02 Download fetches the pinned commit and never executes repository content or runs checkout filters or hooks.
- SK-03 Fetched trees containing symlinks, traversal or reserved names, or exceeding size limits, are rejected.
- SK-04 Activation in a project without an approved `untrusted_tool_install` approval changes nothing on disk.
- SK-05 Deactivation removes exactly the files or MCP entry that activation wrote and leaves user-modified entries alone.
- SK-06 Update keeps the previous ref and rollback restores it.
- SK-07 Entries without a source or that are documentation-only cannot be downloaded and show how to proceed manually.
- SK-08 Codex activation is refused with an explanation.

### Delivery

- DL-01 On a Windows machine with uv and Node, `scripts\start.cmd` prepares the environment on first run, starts the server and opens the dashboard.
- DL-02 `orchestrator doctor` reports the state of Python, Node, git, data directory, token, port, provider CLIs and frontend build without calling a model.
- DL-03 Every documented CLI command works against a running backend and uses the documented exit codes (0 ok, 1 API or usage error, 2 backend unreachable).
- DL-04 `orchestrator stop` terminates only a verified `orchestrator serve` process recorded in the pid file.
- DL-05 The documentation set (README, INSTALLATION, USER_GUIDE, SECURITY, TEST_PLAN, PRD) exists and its commands and screens match the product.
