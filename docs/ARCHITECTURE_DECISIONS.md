# Architecture Decisions

This is a curated summary of the main technical decisions behind AI Orchestrator. Each entry gives the context, the decision and its consequences. The binding module and API contract is in [ARCHITECTURE.md](ARCHITECTURE.md); the threat model and the controls that enforce these decisions are in [SECURITY.md](SECURITY.md).

---

## ADR-01: Local-first modular monolith (FastAPI, SQLite, React)

**Context.** The product is a single-user tool for one Windows machine. It must not require Docker, Redis, a hosted service or any external infrastructure.

**Decision.** One Python process (FastAPI on asyncio) owns all state in one SQLite file and serves the built dashboard. The CLI is an HTTP client of that process. Python 3.12 is managed by `uv`, so nothing is installed globally.

**Consequences.**
- One thing to start, and no divergent CLI state.
- The scale limit is one machine, which is the intended scope.
- The first `uv sync` needs network access; afterwards the tool works offline apart from the provider CLIs.

## ADR-02: Loopback-only API with token authentication and a worker-origin guard

**Context.** Agents run as the same OS user as the orchestrator, so an agent can reach a loopback API and call it with the token (for example, to rewrite the auto-approve setting). Making the token file unreadable was tried and is not sufficient on its own: a worker that can read files outside its workspace could still obtain secrets.

**Decision.** The server binds to loopback only and requires a bearer token (WebSocket: via `Sec-WebSocket-Protocol`, redacted from logs). Every agent, helper and verification child is created suspended, assigned to a per-server Windows Job Object (no breakaway, kill-on-close) and then resumed. Before authentication, the API refuses HTTP and WebSocket requests whose local TCP peer is a process in that job (`403 worker_origin_refused`, WebSocket close 4403). An unresolved loopback peer is refused for state-changing methods. `/api/health` stays open. The OpenAPI and dev origins exist only with `ORCHESTRATOR_DEV=1`.

**Consequences.**
- A worker cannot drive the orchestrator API even when it holds a valid token.
- Processes brokered by the OS (Task Scheduler, WMI, COM, services) are outside the job; this is a documented limit, not a claim of full confinement.
- The guard is Windows-specific; other platforms get a best-effort PID-ancestry check.

## ADR-03: SQLite with atomic transition and event writes; 13-state task machine

**Context.** The system must survive crashes at any point, and the dashboard must never show something that was not recorded.

**Decision.** The standard-library `sqlite3` module is used in WAL mode through one serialized writer, with explicit SQL and `PRAGMA user_version` migrations. The queue is simply the tasks in `QUEUED` state. Every state change writes the task row, a `state_transitions` row and an `events` row in one transaction, then broadcasts. The task lifecycle is a closed state machine of 13 states (`CREATED`, `QUEUED`, `PLANNING`, `RUNNING`, `TESTING`, `REVIEWING`, `WAITING_FOR_QUOTA`, `NEEDS_APPROVAL`, `PAUSED`, `BLOCKED`, `FAILED`, `COMPLETED`, `CANCELLED`) with an explicit transition table in `domain/states.py`; only `COMPLETED` and `CANCELLED` are terminal, and `FAILED` leaves only by explicit retry.

**Consequences.**
- A crash leaves a consistent, replayable history; illegal transitions raise instead of corrupting state.
- No ORM dependency; transactions are explicit and testable.
- `states.py` is contract code: changing it requires updating ARCHITECTURE.md.

## ADR-04: Completion requires deterministic verification

**Context.** A model saying "done" is not evidence that the work is correct.

**Decision.** A task reaches `COMPLETED` only with a stored passing verification record (commands exit 0, file checks pass), or with an explicit user approval recorded as `unverifiable_accepted`. Model self-reports are never sufficient. A provider result that reports success but carries denied actions is classified as a failure: adapters trust observable evidence over a CLI's own status word.

**Consequences.**
- Every completed task carries inspectable evidence of how it was checked.
- Tasks with no checkable outcome need a human decision rather than silently passing.

## ADR-05: One git worktree per task

**Context.** Parallel tasks and different providers must not corrupt each other, and any provider must be able to inspect another's work after a handoff.

**Decision.** Each executing task runs in its own git worktree on its own branch (`orch/<task_id>`), located outside the data directory. Merging back to the base branch is an approval-gated action. Provider-specific worktree flags are not used, so isolation is uniform. Every git call made by the orchestrator uses a hardening prefix: hooks, fsmonitor, pagers, editors, credential helpers, external diff/textconv and configured filters are disabled, and the environment is allow-listed.

**Consequences.**
- Diffs and checkpoints are plain git refs.
- A repository's own hooks and filters do not run during orchestrator git calls.
- A worktree isolates changes; it does not by itself confine a provider process. Confinement is handled per provider (ADR-08, ADR-09).

## ADR-06: Deterministic weighted routing with stored reasons

**Context.** Model selection must be explainable and reproducible, and must never quietly send a task to an unsuitable model.

**Decision.** `router.route` classifies the task with deterministic keyword and metadata rules, filters candidates (installed, authenticated, required capabilities present, usage state usable, policy allows), then scores them with weights from `config/routing.yaml` (tier fit, preference, continuity, usage headroom, cost class). Adaptive adjustment applies only after a minimum sample of verified runs and is bounded and recorded. Capabilities are split into explicit (supplied by the caller) and inferred (detected from task text): an explicit provider preference beats inferred capabilities, while explicit capabilities are never ignored. The decision, with candidates, scores and human-readable reasons, is persisted and shown in the dashboard. If no suitable candidate remains, the task waits or blocks.

**Consequences.**
- "Why this model?" is answerable from stored data.
- Usage state is set only from evidence: a quota signal marks a model only if its own error names it; account-level signals mark the provider; everything else stays `UNKNOWN`.
- Keyword inference can be wrong, which is why an explicit preference may override it.

## ADR-07: Provider adapters behind a contract; subscription CLIs, not paid APIs

**Context.** The target users already have Claude Code, Codex CLI and Antigravity subscriptions. Per-token API billing would be a surprise cost and a different trust model.

**Decision.** Each provider is an adapter implementing the contract in `providers/base.py`. Adapters drive the installed CLIs. Billing classes are `subscription_cli`, `metered_api`, `local` and `external_paid`; only `subscription_cli` and `local` are enabled without explicit approval. Adapters resolve the real executable (a native binary, or `node` plus its JS entry) instead of the `.cmd` shim, pass only fixed, validated flags as an argument array, and send the prompt on stdin. No `cmd.exe` is in the execution path. A recursion guard (`AI_ORCHESTRATOR_DEPTH`) stops an orchestrated agent from launching the orchestrator again.

**Consequences.**
- Task text can never become a shell command, and long prompts do not hit command-line limits.
- Adding a provider means implementing one contract; a mock provider supports tests without spending quota.
- The orchestrator depends on third-party CLI behavior, so adapters detect capabilities at runtime instead of assuming them.

## ADR-08: Claude workers use restricted file tools and no shell

**Context.** A Claude worker with a shell can do anything the user can do.

**Decision.** Every Claude run uses `--restricted`, strict MCP configuration and disabled slash commands. Read-only runs allow only Read, Glob and Grep; workspace-write runs add file-editing tools. Shell, PowerShell and web tools are explicitly disallowed, and generated settings deny access to the data directory. Enabling a shell is an opt-in in the configuration file only; the API rejects the key, and with it Claude is documented as not confined.

**Consequences.**
- Workers cannot run tests or arbitrary commands; the orchestrator's own verification step does (ADR-10).
- A repository's `.claude/settings.json` hooks do not run.
- The approval text states the confinement level per provider instead of implying uniform guarantees.

## ADR-09: Codex runs in an owned runtime behind an evidence-bound activation attestation

**Context.** A Codex run reads user-level configuration, authentication and MCP servers from the user's profile, and its own sandbox is not a read boundary. Metadata probes also showed that an empty override does not remove merged MCP configuration.

**Decision.** Codex executes in an orchestrator-owned runtime: an owned `CODEX_HOME` populated from a bounded subscription snapshot (general user settings are not copied), a pinned configuration, named MXC permission profiles for read and write, and a per-run effective MCP inventory that must be empty. Every inventoried name is disabled with an inline TOML table, and an unreadable, malformed or oversized inventory blocks the task. Hooks, plugins and apps are off. Production execution is refused unless an activation record matches the installed CLI. `orchestrator codex-attest` writes that record only after 12 model-free native containment checks pass, each with a positive control. The record binds the Codex version, a hash of the package and `node.exe`, and a hash of the isolation code loaded at import. A CLI update or a change to the isolation code invalidates it. There is no environment or request bypass and no legacy path.

**Consequences.**
- Codex is unavailable until attested on the machine, and stops being available after an update; this is deliberate.
- Model-free checks prove the sandbox, not that a real model run succeeds, so real execution is accepted separately.
- Raw MCP inventories are never logged because they can contain transport credentials.

## ADR-10: Verification in a per-command AppContainer inside a disposable copy

**Context.** Verification commands run code the agent wrote. Low integrity alone does not deny other tasks' files, user credentials or network access, and running in the real worktree would let agent code alias or swap the files being checked.

**Decision.** For every command the orchestrator builds a private copy of the task tree (copy-in) and runs the command there; nothing is copied back. The copy is made with exclusive creation relative to held directory handles, with no reparse points and single-link files, and the access grant is applied while the copy is still empty. The command runs in a zero-capability, per-task AppContainer whose identity is checked on the suspended child's actual token before resume, with Low integrity, a private desktop and nested Job Objects. Interpreters and tooling come from private, hash-validated copies of the installed Python and Node runtimes, so access to the original installations is never granted. Existing or unknown AppContainer profiles are refused, never adopted. In-place verification is refused.

**Consequences.**
- Agent code cannot reach other tasks, user credentials or the network during verification, and cannot alter the real worktree.
- Compatibility costs: tools that need the original install location, and native build toolchains, are outside the validated scope.
- Per-command copying adds latency; timing diagnostics measure it without relaxing any security budget.

## ADR-11: Process-wide runtime lease: validate once, recheck cheaply

**Context.** Validating a private runtime's complete file set, hashes and ACLs for every command is slow, but a runtime that can change after validation is unsafe.

**Decision.** A shared, process-wide lease validates a runtime once, then holds handles on its files and ancestors so they cannot be written or deleted while any job uses it. Later commands do a cheap handle-level recheck rather than a full revalidation. The lease is released only after the job has drained.

**Consequences.**
- Validation cost is amortized without weakening the guarantee.
- If a handle close is uncertain, the runtime stays owned and further acquisition is refused until restart (ADR-12); a failed handle is never retried by number, because the number may have been reused.

## ADR-12: Fail-closed cleanup with residue quarantine

**Context.** After a crash or an uncertain process exit, a persisted PID cannot prove that it still belongs to the process the orchestrator started, and surviving descendants can outlive a task.

**Decision.** The orchestrator never terminates a process based on a persisted PID guess. Uncertain live workers become a durable retry barrier, and only the exact verified task-owned worktree is quarantined. Per-run Job Objects cover provider, async helper and synchronous git helper paths. A successful result is withheld until descendants have drained and context cleanup has finished, and cleanup uncertainty outranks ordinary cancellation. A stale profile or runtime left by a crash blocks reuse instead of being adopted or deleted by name prefix.

**Consequences.**
- Failures surface as `BLOCKED` tasks with a recorded reason rather than as silent leaks or wrong kills.
- Recovering from some crash states needs an explicit user action.

## ADR-13: Leader and advisor output is data, not instructions

**Context.** A leader model that decomposes work will sometimes propose shell-style verification commands, and any text from a model or fetched content can carry injected instructions.

**Decision.** The leader is a bounded, schema-constrained caller invoked by the deterministic supervisor, not a long-running chat. It emits structured checks (`file_exists`, `contains`, `file_absent`, `file_unchanged`, `json_valid`, `cmd` as argv). Commands are accepted only through per-tool argument allowlists, with default deny and path arguments confined to the worktree. Leader-proposed verification is adopted automatically only if it contains a positive check; otherwise a human approves it. The advisor receives the problem, requirements and evidence rather than the leader's transcript, and its answer is stored as a decision record. Fetched skills, MCP descriptions and web content are treated as untrusted data.

**Consequences.**
- A model cannot turn its output into an executable command outside the allowlists.
- Leadership can be switched between providers with a checkpoint and a validation call, because no transcript is carried over.

## ADR-14: Approvals for risky actions

**Context.** Some actions are irreversible or widen the trust boundary, and unattended operation magnifies mistakes.

**Decision.** Risky actions create an approval request and move the task to `NEEDS_APPROVAL`: merging to the base branch, permission escalation, unverifiable completion, initializing a non-Git project, adopting leader-proposed commands without a positive check, and enabling paid or external providers. Auto-approval is configured only in the settings file and is empty by default. Approval text states what is and is not confined for the chosen provider.

**Consequences.**
- The default path is safe; the cost is interruptions the user must answer.
- Approvals are recorded, so the audit trail shows what was allowed.

## ADR-15: Event-sourced dashboard and a Pixel Office driven by real activity

**Context.** A dashboard that shows inferred state can drift from reality, and decorative animation can mislead about what agents are doing.

**Decision.** The React dashboard is a projection of the append-only event log: it loads a snapshot, then follows the WebSocket stream and resumes by sequence number. The Pixel Office (PixiJS, procedurally drawn original assets) is driven only by those events, so a character moves or works only when a recorded agent activity says so.

**Consequences.**
- The UI cannot show anything that was not recorded, and a reload reproduces the same view.
- The office view is a faithful but simplified rendering; the event log remains the source of truth.

## ADR-16: Native containment instead of a VM or Hyper-V requirement

**Context.** A hypervisor boundary was considered for running verification and provider processes, because a VM is a stronger isolation primitive than a user-mode sandbox.

**Decision.** The product does not require Hyper-V, a VM or any guest operating system. Isolation uses native Windows primitives: AppContainer, mandatory integrity levels, Job Objects, protected ACLs and handle-pinned filesystem operations. The VM option was evaluated and is not required for the supported threat model. Neither approach is claimed to be a proof; each control is claimed only where a native test with a positive control demonstrates it.

**Consequences.**
- Installation stays simple: no virtualization feature, guest image or licence is needed.
- Security claims are limited to what the native tests show, and confinement is described per provider rather than generalized.
- A stronger boundary could be added later behind the same verification interface without changing the domain contract.
