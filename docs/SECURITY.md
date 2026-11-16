# Security

Scope: a local, single-user tool on Windows. This document states what the code enforces, where, and what it does not. Statements about behaviour refer to the code at the time of writing; the backend is still changing, so check the referenced file when it matters. An independent security review was done (17 findings); the hardening described below was implemented in response. A re-review of those fixes has not happened yet (see section 5). Paths below are relative to `backend/orchestrator/` unless they start with `config/` or `frontend/`.

## Current activation boundary (2026-11-15)

Production Codex execution is refused unless an **activation record** matches the installed CLI
(`security/codex_activation.py`, `providers/codex.py`). The record `<data dir>\codex-activation.json`
(owner-only, strict schema) binds the Codex version, a SHA-256 of the whole `@openai/codex` package plus
`node.exe`, and a SHA-256 of the isolation code (codex runtime/auth/activation, provider adapter and process layer, git, paths, ACL and job modules, snapshotted from the loaded modules at import). Only `orchestrator codex-attest` writes it, and only after
12 model-free native containment checks pass, each with a positive control (named MXC profiles: in-task
write allowed; sibling/owned-home read and write denied; read-only profile write denied; untrusted
project MCP not started while a trusted control does start; pre-planted hardlinks refused before auth
creation; worker-created hardlinks/junctions give no read/write to outside objects, probed in an unguarded
subdirectory where the host can create the same link and the worker can write; observed: the profile denies
link creation entirely, even in-task). A denial counts only for a completed process (a timeout is a harness
failure), and the sandbox positive control is repeated after each group of denials. A Codex update or a
change to the isolation code invalidates the record. There is no request/env bypass and no legacy path.
These are model-free sandbox checks: they are not evidence that a real model run succeeded; real
exec/resume acceptance (ER-19/ER-20 in [PRD.md](PRD.md)) is tracked separately in [VERIFICATION.md](VERIFICATION.md).

The owned Codex implementation reads a bounded subscription snapshot into a protected temporary
runtime; it does not copy general user settings. Credentials must never appear in release files,
logs or public test evidence. Cleanup uncertainty fails closed. A successful text-only independent
review or CLI discovery is not a real coding/isolation PASS.

## 1. Threat model

Assets: the files of your registered projects, your provider CLI logins (which live in the CLIs' own stores), the API token in `api-token` and the database `orchestrator.db`, both in the data directory (default `%LOCALAPPDATA%\AI-Orchestrator`, see section 3).

What the product can do on your machine: start AI agents that edit files (and, depending on the provider, run commands) inside task worktrees, and run verification commands. Anyone who can call the API can therefore cause code execution as your Windows user. The security model is built around keeping the API reachable only by you.

| Threat | Relevant in this setting? | Main controls |
|---|---|---|
| Another machine on the network reaches the API | Yes if the bind is changed | Loopback bind; CLI refuses a non-loopback host without an explicit flag |
| A web page in your browser calls the local API (CSRF, DNS rebinding) | Yes | Host and Origin validation; bearer token required; no cookies |
| Another local user reads the token or database | Yes | Data directory, worktrees directory and token file get an owner-only ACL (`security/acl.py`); the data directory lives outside the repository; token not printed by the CLI |
| An agent or verification process started by the orchestrator calls the orchestrator's own API (for example to change settings or `auto_approve`) | Yes | Worker-origin request guard: such processes run in a Windows Job Object and the API refuses their requests before authentication (section 2, "Worker-origin request guard") |
| A prompt injection in repository files, web content, tool output or a skill makes an agent misbehave | Yes | Least-privilege run profiles (provider-specific, see "Worker permission profiles"), external content labelled as data, approvals; no guarantee against a model following hostile text inside its permissions. Worktree confinement is verified for Claude and, for writes only, for Codex; not for reads by Codex, and not determined for Antigravity |
| A malicious or compromised skill or MCP server | Yes | Fetch without checkout or execution, static inspection, approval before activation, project-scoped activation |
| A leader or advisor model proposes a dangerous verification command | Yes | Safety filter; unsafe proposals need a human approval |
| Secrets end up in logs, events or the UI | Yes | Redaction on events, verification output and server log lines |
| A malicious local user with admin rights, malware already running as you | Out of scope | The ACL keeps other local accounts out; it does not protect against processes running as the same user |
| Compromise of a provider vendor or CLI | Out of scope | |

## 2. Controls and where they are enforced

### Network exposure

- Loopback bind. The default host is `127.0.0.1` (`config/policies.yaml`, `security.bind_host`). `orchestrator serve --host` rejects a non-loopback address unless `--i-understand-exposing-the-dashboard` is passed (`cli/main.py`, `_is_loopback` and the check around line 120). Test: `tests/cli/test_cli_core.py::serve_refuses_non_loopback_without_the_flag`.
- Host and Origin validation for HTTP and WebSocket (`api/security.py`, `HostOriginMiddleware`). Allowed hosts are `127.0.0.1`, `localhost`, `[::1]` (`api/app.py`, `DEFAULT_ALLOWED_HOSTS`). Allowed origins come from `config/policies.yaml` (`security.allowed_origins`) plus the loopback origins of the port the server runs on. A request without an `Origin` header (CLI, curl) is accepted but still needs the token. Unexpected values give HTTP 403 or WebSocket close code 4403. Tests: `tests/security/test_api_security.py`.

### Authentication

- Bearer token. `security/auth.py` creates a random 32-byte URL-safe token in `api-token` in the data directory on first start (exclusive create, mode 0600, plus the owner-only ACL described in section 3), compares it in constant time (`hmac.compare_digest`) and registers it with the redactor. Every route under `/api` except `GET /api/health` depends on `require_token` (`api/deps.py`). Test: `tests/security/test_api_security.py`.
- WebSocket `/ws` (`api/ws.py`) accepts the token in either of two ways: the subprotocol `Sec-WebSocket-Protocol: orch, orch.token.<token>`, or the `?token=` query parameter (still accepted for compatibility). The shipped dashboard (`frontend/src/store/stream.ts`) and `orchestrator follow` use the subprotocol, so the token is not in the WebSocket URL. A wrong token closes the socket with code 4401. Slow clients are closed with 4408. Tests: `tests/security/test_token_leak.py`.
- The CLI reads the token from the token file, never accepts it as an argument and never prints it (`tests/cli/test_cli_core.py::token_is_not_accepted_on_the_command_line`, `token_never_printed_anywhere`). `orchestrator dashboard` and `serve --browser` open `/#token=<token>`: the token is in the URL fragment, which the browser never sends to the server; the page stores it in `sessionStorage` and removes it from the address bar (`frontend/src/api/auth.ts`).

### Worker-origin request guard

- Problem: an agent (or verification command) runs as your Windows user and could otherwise call `http://127.0.0.1:<port>/api/...` with the token, for example `PUT /api/settings` to fill `auto_approve`. Hiding the token file alone is not sufficient: a Codex worker can read files outside its workspace.
- Mechanism (Windows; `security/worker_job.py`, `security/peer.py`, `api/security.py`). Every agent, helper and verification child process is created suspended, assigned to a per-server Job Object (no breakaway, kill-on-close) and then resumed. For each HTTP and WebSocket request the API resolves the local TCP peer's process and refuses it when it is in that job: HTTP 403 `worker_origin_refused`, WebSocket close code 4403, before authentication. A loopback peer that cannot be resolved is refused for state-changing methods and for WebSocket (403 `peer_unresolved`). `GET /api/health` stays open.
- Verified with a real Codex task under the real server: the worker's `GET /api/status` and `PUT /api/settings` both returned 403 and `auto_approve` stayed empty. Tests: `tests/security/test_sec_01_worker_origin_guard.py`, `tests/security/test_sec_01_token_reach.py`.
- Limits: processes started through a broker (Task Scheduler, WMI, COM/ShellExecute into an existing process, services) are outside the job. A worker that can read files (Codex) can still read the SQLite database and possibly the token file, and could copy secrets into its output. Verdicts are cached for 1 second per client endpoint. On POSIX the check is best-effort (PID ancestry). In the same real run the Codex worker could not read the token file inside the ACL-hardened data directory; the cause was not determined, so this is not relied on as a control.

### Path confinement

- `security/paths.py` rejects empty paths, NUL bytes, UNC and device paths, NTFS alternate data streams, drive-relative paths and traversal, and resolves symlinks and junctions with `realpath` before checking that the target is inside an allowed root. `validate_root` requires a project path to be an absolute, existing directory.
- Project registration additionally refuses drive roots, the user profile and its parent, `%APPDATA%`, `%LOCALAPPDATA%` and `%ProgramData%` themselves and anything containing them, anything in or containing `%SystemRoot%` or Program Files, and anything that overlaps the data or worktrees directory (junctions and 8.3 names are resolved). Test: `tests/security/test_sec_07_project_roots.py`.
- A run whose working directory or extra directory is, is inside, or contains the data directory is refused for every adapter (`tests/security/test_sec_03_04_confinement.py`).
- Used for: project registration (`services/projects.py`), the working directory of every agent run (`scheduler/executor.py`, `resolve_inside(cwd, roots)`), the diff and artifact endpoints (`api/routes/tasks.py`, `resolve_inside(path, [wt])`), static file serving of the dashboard (`api/app.py`), skill activation targets (`skills/artifacts.py`) and knowledge indexing (`knowledge/sources.py`). A violation returns HTTP 403 `path_policy`. Tests: `tests/security/test_paths.py`, `tests/security/test_api_security.py::artifact_path_traversal_rejected`.

### Subprocess execution

- Argument arrays, no shell string. Provider processes are started with `asyncio.create_subprocess_exec` and an argv list (`providers/process.py`); the prompt is sent on stdin, never in argv (`tests/contract/test_provider_contract.py::prompt_never_in_argv`). npm `.cmd` shims for `claude` and `codex` are resolved to the real executable or `node` plus the JS entry so `cmd.exe` argument parsing is not involved; an unparsable shim fails instead of falling back to `cmd.exe` (`providers/process.py`, `resolve_executable`; `tests/contract/test_process.py`).
- Recursion guard. Children get `AI_ORCHESTRATOR_DEPTH` incremented; an adapter refuses to start when it is already 1 or more (`providers/process.py`, `check_recursion_guard`; `tests/contract/test_process.py`, `test_provider_contract.py::recursion_guard_refuses_nested_start`). Claude runs also use `--restricted`, `--strict-mcp-config` and `--disable-slash-commands` (`providers/claude.py`).
- Child environment allowlist. Children receive only the variables named in `config/policies.yaml` (`security.child_env_allowlist`, which includes `CODEX_HOME` and `CLAUDE_CONFIG_DIR` so the CLIs find their logins) plus the depth variable; no `ORCHESTRATOR_*` variable is ever passed on (`providers/process.py`, `build_child_env`; `tests/contract/test_process.py::secret_does_not_reach_real_child`). Verification commands use the same allowlist (`scheduler/verification.py`, `Verifier`).
- Process tree termination. Cancel, timeout and abandoned runs kill the whole tree (`providers/process.py`; `scheduler/verification.py::kill_tree`; tests `terminate_tree_kills_child_and_grandchild`, `adapter_cancel_real_process_tree`).
- Git hardening. Every git call made by the orchestrator uses a protective prefix: hooks off (`core.hooksPath`), fsmonitor off, no pager, editor, askpass, ssh command or credential helper, `protocol.ext.allow=never`, and configured clean/smudge/process filters blanked; diffs use `--no-ext-diff --no-textconv`, commits use `--no-verify`, and the environment is an allowlist with no inherited `GIT_*` variable (`gitops/git.py`; test `tests/security/test_sec_6_git_hardening.py`). A hook or filter defined in a project repository therefore does not run when the orchestrator calls git.
- Worker job. Agent, helper and verification processes run in the per-server Job Object described under "Worker-origin request guard"; closing the server kills them.
- Single instance. `server.lock` in the data directory is an OS-level lock; a second `orchestrator serve` on the same data directory fails fast, before recovery, with one clear line (`security/instance_lock.py`; test `tests/security/test_sec_10_instance_lock.py`).
- Session ids passed to `--resume` / `--conversation` are validated (`[A-Za-z0-9._:-]`, 1 to 128 characters, no leading `-`) and passed in the attached `--resume=<id>` / `--conversation=<id>` form (test `tests/security/test_sec_13_session_ids.py`).
- Skill fetching runs git with protective options (section "Skills" below).
- `orchestrator stop` terminates only a process whose pid file and command line match `orchestrator serve` (`cli/server.py`; `tests/cli/test_cli_core.py::stop_*`).

### Worker permission profiles

- Two profiles exist in the contract (`ExecutionRequest.permission`): `read_only` and `workspace_write` (`config/policies.yaml`, `worker_permissions`). Plan, review and advise runs are read-only; execute and repair runs are workspace-write inside the task worktree (`scheduler/executor.py`; test `tests/m2b/test_routing_flow.py::plan_phase_is_read_only`).
- Per provider mapping. The claims differ by provider and are stated as verified on this machine:
  - Claude (`providers/claude.py`, verified with real runs on claude 2.1.322). Every run uses `--restricted --strict-mcp-config --disable-slash-commands`. Read-only allows Read, Glob and Grep. Workspace-write allows file tools only (Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep): no shell and no web tools by default (`--disallowedTools` Bash, PowerShell, WebFetch, WebSearch), and `--settings` carries deny rules for the data directory. Observed: a write outside the worktree was refused, a read of a token file outside the worktree was refused, the shell was not available, a hook in a repository `.claude/settings.json` did not run, and the subscription login still worked. `--restricted` also refuses writes to settings, git and tool-configuration files. Consequence: Claude workers cannot run tests or commands themselves; the orchestrator's verification step runs them. Tests: `tests/security/test_sec_03_04_confinement.py`, `tests/contract/test_provider_contract.py`.
  - Claude with shell (opt-in). `worker_permissions.claude_shell: true` in `config/policies.yaml` adds Bash. It is YAML-only: the API rejects the key with 422 and a value in the settings table is ignored. With it enabled, Claude is NOT confined.
  - Codex (`providers/codex.py`): its own sandbox, `-s read-only` or `-s workspace-write` (a `sandbox_mode` config override when resuming), plus `--ignore-rules`, `--disable hooks`, `--disable plugins` and MCP servers disabled. Verified with real runs: writes outside the workspace were refused except under `%TEMP%`; the worker could read a file outside the workspace, has a shell, and could reach a loopback HTTP server. So Codex is write-confined by Codex itself (worktree plus temp), not read-confined. No flag exists to ignore a project-level `.codex/config.toml`.
  - Antigravity (`providers/antigravity.py`): `--mode plan` or its own `accept-edits` mode, plus `--disable-slash-commands`. Headless shell commands are auto-denied by `agy`; the adapter reports `permission_denied` for that case. What it may read and whether it loads workspace configuration are not determined.
- Permission-skip flags are never used (`tests/contract/test_provider_contract.py::dangerous_flags_never_used`).
- A denied action creates a `permission_escalation` approval. Its text is provider-specific and states the above truthfully. Approving can only grant `workspace_write` for the next run of a read-only phase; it never grants network access, access outside the worktree, or unrestricted shell (`security/approvals.py`, `permission_escalation_text`).
- Codex isolation of the user's tools. Each run inspects effective MCP metadata from its working directory
  with hooks/plugins disabled, then uses a quoted inline TOML table to disable every server name, including
  names containing dots. Unknown or oversized inventory blocks model startup rather than using stale names.
  `tests/security/test_codex_mcp_safety.py` checks native installed CLI metadata against fresh user/project
  fixtures without starting a model or server. This is not proof for real exec/resume, and configuration is
  not held immutable between inspection and the later CLI load. Its effect on real token use is unmeasured.

### Approval gates

- `security/approvals.py` (`ApprovalService`) creates approvals, emits `approval.requested` and, on resolution, re-queues the task (approve) or blocks it with the reason (deny). The kind `unverifiable_completion` completes the task and records `unverifiable_accepted`. `auto_approve` in policy (empty by default) can pre-approve a kind. Tests: `tests/security/test_approvals.py`.
- Kinds created by code today: `unverifiable_completion` and `git_init_in_project` (`security/approvals.py`, `scheduler/`), `permission_escalation`, `high_risk_task` (critical risk per `config/routing.yaml`), `untrusted_tool_install` (`skills/manager.py`), and the leader gates `leader_verify_commands`, `leader_clarification`, `leader_escalation`, `leader_budget` (`leader/controller.py`).
- Kinds listed in `config/policies.yaml` for which no code path creates an approval at present: `destructive_file_delete`, `destructive_git`, `deployment`, `purchase`, `metered_api_activation`, `sensitive_external_action`, `high_privilege_command`, `merge_to_base_branch`. They are not enforcement today. In practice the orchestrator itself performs no deployment, purchase, merge or branch deletion; agents run with the permission profiles above, which are the actual boundary for destructive actions inside the worktree.

### Event retention and replay

- Events are pruned by age and row count (defaults 30 days and 200,000 rows; `limits.events_max_age_days`, `limits.events_max_rows`; events of unfinished tasks are kept) (`db/retention.py`; `tests/integration/test_sec_15_events_retention.py`).
- WebSocket replay is capped at 2,000 events (`limits.ws_replay_max_events`). When more are pending the server sends a `replay.truncated` control frame and marks `replay.done` with `truncated: true` (`api/ws.py`).

### Dev-only surface

- `/docs`, `/redoc`, `/openapi.json` and the `5173` development origins exist only when the server is started with `ORCHESTRATOR_DEV=1` (needed for `npm run dev`). Test: `tests/security/test_sec_16_dev_surface.py`.

### Redaction

- `security/redact.py` masks known secret formats (private keys, bearer headers, Anthropic, OpenAI, Google, GitHub, GitLab, Slack, AWS and npm token shapes, JWTs, URL credentials, `key=value` for secret-like names) and registered exact values such as the API token. The event bus redacts every payload before it is stored or broadcast (`events/bus.py`); verification output and the artifact and diff endpoints are redacted as well (`scheduler/verification.py`, `api/routes/tasks.py`). Knowledge indexing redacts before storage and skips secret-looking files (`knowledge/sources.py`). Tests: `tests/security/test_redact.py`, `tests/knowledge/test_knowledge_indexing.py::secrets_redacted_before_storage`. Pattern-based redaction cannot catch every secret format.
- Server logs. `security/logfilter.py` rewrites `token=<value>` query values in uvicorn log records and is attached to uvicorn's handlers (`api/app.py`, `install_log_filter`; `cli/` passes the log config to uvicorn). The background server also runs with the HTTP access log disabled. Tests: `tests/security/test_token_leak.py`.

### Verification command safety

- Commands use argv lists, bounded captured output, a timeout, no stdin and a stripped environment
  (`scheduler/verification.py`, `run_command`). Windows verification requires an exact per-task AppContainer
  identity with zero capabilities, no loopback exemption, a Low token, private desktop and nested jobs.
  Isolation setup failure blocks execution; no Low-only or unsandboxed fallback exists. Python runs from a
  validated private copy held against modification through child exit. Standard Program Files Node/npm uses
  the same private-copy lease, empty user/global npm configuration and private cache. npx uses installed local
  dependencies offline. Existing Python/Node/profile/system ACLs are untouched; the original npm installation
  is not made accessible. In-place command checks are refused. Unsupported runtimes fail closed.
  Shell operators outside quotes are rejected by the command splitter.
- Copy-in verification (`security/verify_tree.py`). A command never runs in the task worktree. Each command
  gets a fresh orchestrator-owned run directory `<worktrees>\_sandbox\<project>\runs\<id>\` under a protected
  `runs\`; its `tree\` is granted to a per-command AppContainer SID and Low-labelled while still empty, then
  filled from the worktree through no-follow handles relative to held parents (regular files only, link count
  1, reparse points and unsafe `.git` refused). Nothing is copied back; the run directory is removed through
  handles after the job drains, and anything that cannot be removed is moved to a quarantined `residue\`
  area. Worktrees are never granted to a sandbox or Low-labelled, so aliases a command creates can never be
  followed later by Git, a provider or a later verification. Consequence: commands do not share files with each
  other (a build in one command is not visible to the next) and outputs never reach the worktree.
- Commands proposed by the leader are data. `leader/safety.py` accepts only allowlisted entry points (pytest, `python -m` with a few modules, `npm`/`pnpm`/`yarn` with `test`, `lint`, `build`, `typecheck`, `check`, and the read-only git forms `git diff --exit-code`, `git status --porcelain`, `git ls-files`), rejects shell metacharacters, code-execution flags (`-c`, `-e`, `--eval`, `--exec`, `--require`, `--import`, and similar), paths outside the worktree and anything with `~` or absolute paths outside it, and caps counts and lengths. Structured file checks (`file_exists`, `contains`, `file_absent`, `file_unchanged`, `json_valid`) are executed by the verifier itself without starting a process. Anything else goes to a `leader_verify_commands` approval and is never run automatically. Tests: `tests/leader/test_leader_plan_review.py::safety_filter_rules`, `tests/leader/test_structured_checks.py`.

### Skills and MCP fetch and activation

- Source policy: only `https://github.com/<owner>/<repo>[/tree/<ref>/<path>]` (`skills/sources.py`, `GithubSourcePolicy`).
- Fetch without checkout or execution: a throw-away repository with an empty template, `core.hooksPath` pointing to the null device, no credential helper, no submodule recursion, no LFS filters; the pinned commit is fetched, its tree is listed with `git ls-tree` and validated entry by entry, and blobs are written by the orchestrator via `git cat-file --batch`. No `git checkout`, so no smudge or clean filter, `.gitattributes` driver or hook can run. Symlinks, traversal, reserved and alternate-data-stream names and over-limit trees are rejected. Tests: `tests/skills/test_skills_install.py` (`repo_content_is_never_executed`, `fetch_uses_protective_git_config`, `symlink_entry_rejected`, `unsafe_paths_rejected`, `size_and_file_count_limits`).
- Static inspection (`skills/static_inspect.py`) flags native binaries, script files, package scripts, hooks and MCP servers and sets `NEEDS_SECURITY_REVIEW`. Scripts found in a package are never run by the orchestrator.
- Activation is project-scoped only; the manager refuses any other scope and never writes global configuration (`skills/manager.py`, `activate`). It needs an approved `untrusted_tool_install` approval for the exact ref and project; without it nothing is written (test `tests/skills/test_skills_activation.py::activate_without_approval_returns_202_and_changes_nothing`). Target paths are checked with `resolve_inside` against the project root (`skills/artifacts.py`). The activation approval shows the exact MCP command, its arguments and the names of its environment keys, and is bound to a content hash that is re-verified at activation; older approvals no longer authorise (`tests/skills/test_sec_17_approval_binding.py`). Reinstall and removal confine the marker's file list to the skill's own install directory (`tests/skills/test_sec_12_marker_confinement.py`). It refuses to overwrite a foreign folder or an existing MCP entry and leaves a user-modified entry in place on deactivation. Updating removes the old ref's project copies and requires a new approval to activate the new ref; rollback restores only for projects that had an approval for the previous ref.
- The skill text shown in the dashboard is untrusted content rendered as plain text (`frontend/src/views/skills/Drawer.tsx`).
- Installing a skill that an agent then reads can still influence that agent. Activation approval is the control; review the skill first.

### External content treated as data

- Leader, advisor and switch prompts state that repository listings, plans, evidence and checkpoints are data and not instructions (`leader/controller.py`, `leader/objectives.py`, `leader/advisor.py`, `leader/switch.py`). Model outputs must parse against JSON schemas; invalid output gets one repair attempt and then fails (`leader/schemas.py`).
- Retrieved knowledge is wrapped with an "UNTRUSTED REFERENCE DATA" label and a header telling the model not to follow instructions inside it (`knowledge/retrieval.py`). The knowledge indexer writes only to its own tables (source-scan test in `tests/knowledge/test_knowledge_indexing.py`).
- The leader's review must rest on recorded verification; an "approve" verdict alone cannot complete a task (`tests/leader/test_leader_plan_review.py::review_approve_still_needs_passing_verification`).
- This is a mitigation, not a guarantee: a model can still be influenced by text it reads inside the permissions it was given.

## 3. Data at rest

The data directory is not encrypted. Default `%LOCALAPPDATA%\AI-Orchestrator` (non-Windows: `$XDG_DATA_HOME` or `~/.local/share`, plus `/ai-orchestrator`); `ORCHESTRATOR_DATA_DIR` overrides it. It holds `orchestrator.db`, `api-token`, `logs\`, `skills\`, `server.pid` and `server.lock`: the API token, the database with events (redacted) and task descriptions, and fetched skills. Task worktrees (your code) are in a separate sibling directory, `%LOCALAPPDATA%\AI-Orchestrator-worktrees` (or `ORCHESTRATOR_WORKTREES_DIR`); leader and advisor scratch is `<worktrees>\_scratch\leader`. Startup refuses a layout where one of the two directories contains the other. The repository's `data\` folder is no longer used.

The data directory, the worktrees directory and the token file get an owner-only ACL (inheritance removed; current user, SYSTEM and Administrators) via `icacls` (`security/acl.py`); `orchestrator doctor` checks it, and `ORCHESTRATOR_HARDEN_ACL=0` disables the hardening (used by tests). This keeps other local accounts out. It does not protect against processes running as the same user. Tests: `tests/security/test_sec_08_acl.py`.

There is no automatic migration from the old location; `orchestrator doctor` warns if a legacy `<repo>\data\orchestrator.db` exists. Provider logins are not stored by the orchestrator; they stay in the CLIs' own stores. Apply disk encryption if you need protection at rest.

## 4. Guidance for the user

- Do not expose the port. Do not use `--i-understand-exposing-the-dashboard` on a shared network, and do not put the server behind a reverse proxy or a port forward.
- Treat `api-token` (in the data directory, `%LOCALAPPDATA%\AI-Orchestrator` by default) like a password. If it may have leaked, stop the server, delete the file and start again to get a new token.
- Do not publish backups of the data directory. Do not register projects you do not trust: verification runs their code in an AppContainer sandbox (see above), but provider agents and standard AppContainer residuals (brokers, readable system locations) are not a complete isolation boundary.
- Do not enable `worker_permissions.claude_shell` unless you accept that Claude workers are then not confined.
- Read a skill (source, `SKILL.md`, security report, scripts) before approving `untrusted_tool_install`. Read the exact list of commands in a `leader_verify_commands` approval before approving.
- Read what a `permission_escalation` approval grants (it says so in the summary, specific to the provider) before approving.
- Prefer Claude for write tasks on repositories you do not fully trust; Codex is not read-confined and Antigravity's confinement is not determined.
- Register only projects you are willing to let an AI agent edit; keep them under git so every change is on a branch you can inspect and discard.
- Keep the provider CLIs updated and logged in with the account you intend to use.

## 5. Known weaknesses and open items

The original review and takeover follow-ups received independent read-only re-review.
Finding N2 remains open; targeted evidence does not replace the final
security/backend/frontend/end-to-end gates. The list below describes the current integration.

1. Verification runs in a per-command AppContainer inside a disposable copy (copy-in, above), plus the Low
   token and per-command job/desktop. Post-launch aliases, scan-to-use races and cross-run cache sharing are
   excluded by construction rather than by scanning. Remaining residuals: standard AppContainer broker channels
   and locations readable by ALL APPLICATION PACKAGES (system directories), unsupported original npm scripts and
   in-place command checks, crash-left AppContainer profiles (refused, not adopted), residue that a hostile
   command makes undeletable (quarantined, retried with backoff). Network loopback denial is tested for TCP/UDP.
   Do not run unattended on untrusted repositories; keep `auto_approve` empty.
2. Codex confinement depends on the execution path. The owned path (the only one allowed in production) runs
   Codex with an orchestrator-owned `CODEX_HOME`, a pinned configuration with named Windows MXC permission
   profiles, and an empty effective MCP inventory; model-free native checks show in-task writes allowed and
   sibling-worktree / owned-home reads and writes denied. Codex still has a shell inside that sandbox, and
   network/loopback reachability under the profile is NOT asserted (the worker-origin guard still refuses
   its API requests); reads of other locations not covered by the checks are not claimed denied. Older
   observations (legacy `-s workspace-write`: writes confined, reads not confined, loopback reachable) describe
   the retired path. Whether real model runs stay inside the profile is recorded per release in [VERIFICATION.md](VERIFICATION.md).
3. Antigravity confinement is not determined. Headless shell commands are auto-denied, but what `agy` may read and whether it loads workspace configuration are untested. `agy` has no authentication status command, so authentication is "unknown" until a call succeeds. Do not route sensitive write tasks to it.
4. Worker-origin guard limits. Processes started through a broker (Task Scheduler, WMI, COM/ShellExecute into an existing process, services) are outside the job; a 1-second verdict cache applies to GET/HEAD only (state-changing requests and WebSocket upgrades are re-resolved every time); forwarding headers (`X-Forwarded-*`, `Forwarded`, `X-Real-IP`) are refused on loopback connections and uvicorn is started with proxy headers off — if you launch uvicorn yourself, pass `--no-proxy-headers --forwarded-allow-ips ""`; POSIX is best-effort (PID ancestry). The guard is verified on Windows only. Whether a Codex worker can read the token file inside the ACL-hardened directory was seen to fail once and the cause is unknown, so it is not a control.
5. Claude with `worker_permissions.claude_shell: true` is not confined. The default (no shell) is verified; Claude workers then cannot run tests, which the verification step does.
6. The ACL does not protect against processes running as your user. It excludes other local accounts only. Same-user malware is out of scope.
7. `.cmd` and `.bat` verification commands go through `cmd.exe`. A verify command such as `npm test` resolves to `npm.cmd`, which Windows runs through `cmd.exe`. The code refuses arguments containing `& | < > ^ % ! "` or line breaks for such commands (`scheduler/verification.py`, `_CMD_META`) but this is a filter, not a proper quoting layer.
8. Approval kinds without enforcement. Several kinds in `config/policies.yaml` are not produced by any code (see "Approval gates"). They do not provide protection today. Merging a task branch into the base branch is a manual git action, outside the orchestrator.
9. Provider profiles remain provider-specific. Verification's AppContainer boundary does not make Codex or
   Antigravity read-confined. Every managed provider/helper has a separate lifetime job; descendant exit is
   verified before worktree reuse. Unknown exit quarantines the tree against further Git/worker activity.
10. Redaction is pattern-based and can miss secrets in unusual formats. Event payloads and verification output are redacted; the content of your repository files is not altered. The `?token=` query form is still accepted by the server for compatibility; the shipped clients no longer use it, and a foreground `orchestrator serve` logs requests unless `--no-access-log` is given (token values in uvicorn records are still scrubbed).
11. Real GitHub skill fetch has never been run; the fetch code is tested against local sources only.
12. Skill activation writes into the project's `.claude/skills` or MCP configuration. A skill that is active in a project is read by the Claude agent running there; the approval (now showing the exact command and bound to a content hash) is the barrier.
13. Single-user assumption. There is no per-user authorisation: whoever holds the token can use every endpoint, including settings changes (for example `allowed_origins`, policy limits and `auto_approve`).
14. Takeover fixes received independent read-only review and follow-up review. This is a bounded code review,
    not a general proof of Windows isolation. Current evidence is summarised in [VERIFICATION.md](VERIFICATION.md).
15. The installation, configuration, Python environment, dashboard build and their ancestors cannot be registered
    as projects. Filtering/checkpoint Git operations refuse `include.path` / `includeIf` configuration, without
    editing the user's Git settings. Git also rejects payload junctions, symlinks and hardlinks before tree I/O.
    Providers and verification hold exclusive reservations; unknown helper exit quarantines source/affected trees.

There is no formal vulnerability disclosure process; the project is a single-developer local tool.
