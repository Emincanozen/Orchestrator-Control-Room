# User Guide

This guide explains how to use AI Orchestrator day to day, from the dashboard and from the command line. For installing it and fixing setup problems, see [INSTALLATION.md](INSTALLATION.md). For what is verified and what is not, see [VERIFICATION.md](VERIFICATION.md).

Terms used here:

- **Project**: a folder on your disk (normally a git repository) that the agents may work in.
- **Task**: one unit of work for one project, with optional acceptance criteria and verification commands.
- **Objective**: a goal in plain words. The leader model splits it into tasks.
- **Provider**: one of the AI command-line tools: Claude Code (`claude`), Codex (`codex`), Antigravity (`agy`).
- **Verification**: checks that the orchestrator itself runs (commands, file checks). A task is complete only when verification passes, not when a model says it is finished.

## 1. Starting and stopping

Start: double-click `scripts\start.cmd`, or run it in a terminal. The first start takes a few minutes. It starts the server in the background and opens the dashboard in your browser.

Stop: `scripts\stop.cmd` (or `scripts\orchestrator.cmd stop`).

Other ways to start:

```
scripts\orchestrator.cmd dashboard                  # background server + browser
scripts\orchestrator.cmd dashboard --port 9000 --no-browser
scripts\orchestrator.cmd serve                      # foreground server; Ctrl+C stops it
```

The server is only available while it runs and the PC is on. Tasks that are running when you stop the server are re-queued or blocked the next time it starts (section 14).

## 2. Opening the dashboard and the token

Every request to the server needs a secret token, stored in `api-token` in the data folder (`%LOCALAPPDATA%\AI-Orchestrator` by default). `orchestrator dashboard` opens the browser at an address that carries the token in its `#token=` fragment (the browser never sends the fragment to the server); the page stores it for that browser tab and removes it from the address bar. If you close the tab and open the plain address `http://127.0.0.1:8765/`, the dashboard shows an "unauthorised" screen: run `scripts\orchestrator.cmd dashboard` again to get a fresh link.

Do not share the token or paste it anywhere. If you need to enter it by hand, it is the content of the `api-token` file in the data folder.

The top bar shows:

- the connection to the event stream: `live`, `connecting`, `syncing`, `reconnecting` or `disconnected` (after a restart of the server it reconnects by itself);
- the current leader (`provider / model`, or `unknown` while it is not yet recorded);
- the number of running tasks;
- the number of pending approvals (click it to open Approvals).

The left menu has eleven screens.

## 3. Tour of the screens

### Office

A pixel-art office with one seat per agent (worker, leader, advisor). The animation of each seat comes only from recorded events: idle, planning, coding, testing, reviewing, consulting, rate-limited, needs approval, blocked, celebrating (shortly after a verified completion). Click an agent to open the inspector with its provider, model, current task and current operation. Nothing here calls a model.

### Mission Control

The main working screen. It opens by default.

- **Projects panel (left)**: lists registered projects. `Add project` registers a folder (name, absolute path, optional default verify commands). The pencil edits a project (the path cannot be changed); the bin removes the registration only, never the files on disk.
- **Task board**: one column per task state, with a search box (title, description, id) and a state filter. Each card shows the title, priority and the actions allowed for its state.
- **`New task`**: opens the task form (section 5). **`Objectives`**: opens the objectives list and `New objective` (section 8).
- **Task detail** (click a card). Buttons: Start, Pause, Resume, Cancel, Retry; a button is disabled when the task's state does not allow it. Tabs:
  - **Overview**: state, project, worker, provider and model, routing decision, active time, current operation, verification result, next action, remaining and completed steps, acceptance criteria, verify commands, dependencies, failures, branch, usage.
  - **Log**: live output of the run, tool calls, state changes and verification results.
  - **Timeline**: every state transition with time, actor and reason.
  - **Runs**: each provider execution attempt (phase, provider, model, status, error category).
  - **Checkpoints**: automatic, handoff and manual resume points. You can create a manual checkpoint and restore one, but only while the task is not running (pause it first). Restoring asks for confirmation.
  - **Diff**: the changes the task made in its worktree compared with its base commit.
  - **Verification**: every verification record with the individual checks, exit codes and output.
  - **Artifacts**: the files the task changed in its worktree, with their content viewable (secrets redacted, large files refused).

### Approvals

Human gates. Each pending approval shows what is being asked and, depending on its kind, the details: skill install details and security findings, permission escalation (what approving really grants), high-risk task reasons, or the leader's proposed verification commands. `Approve` or `Deny`, with an optional note. Approving re-queues the linked task (or completes it, for an unverifiable completion). Denying blocks the task with the reason.

### Leadership

Who leads and who advises.

- **Leader**: the current leader model and a picker for a new one. Candidates that are not eligible are disabled with the reason. `Switch leader` lists the steps; the backend validates the new leader and rolls back if validation fails.
- **Advisor**: advisor model (or "let the router choose") and its policy: `never`, `on_request`, `auto` (also consulted automatically after repeated failed repair cycles).
- **Fallback and budgets**: fallback policy (`off`, `same_provider_lower_tier`, `any_capable`) and limits: consultations per day and per task, leader calls per task, parallel workers.
- **Ask the advisor**: problem, optional task, options, constraints. One consult is one model call and counts against the budgets.
- **Decision records**: each advisor answer is stored with context, options, rationale and evidence. A decision that cites no verification, commit or diff is flagged as not backed by objective evidence. A decision can be exported as an ADR entry into the project on request.
- **Permissions and approvals**: which approval kinds exist and which are auto-approved (read-only here).

### Providers & Usage

- **Providers**: for each CLI: installed, version, executable, authenticated (yes, no, or unknown), billing class, when it was last checked, capabilities.
- **Models**: discovered models with tier and capabilities; filters by provider, tier and capability. A capability marked "verified" was confirmed; others are only listed by the CLI or only configured.
- **Usage observations**: the usage state per provider and model with the observed values. See section 12 for what the chips mean.

### Routing

- **Preferences**: provider order, disabled providers, disabled models. Each value shows whether it comes from `config/routing.yaml` or was set by you, and can be reset to the default.
- **Preview a routing decision**: type a title (and optionally description, task type, role, phase) to see which model would be picked. Nothing is run, queued or recorded.
- **Why this model?**: for any past decision: classification (type, complexity, risk, required capabilities), ranked candidates with a score breakdown, rejected candidates with reasons, fallback information, and whether adaptive routing was applied.

### Analytics

Measured from recorded tasks, runs and verifications. Filters: project, provider, model, task type, date range. Key figures: tasks completed, verified completion rate, test pass rate, retries, repair cycles, run duration (median and 90th percentile), task active time, provider/model switches, handoff checkpoints, estimated cost, provider availability. Charts: tokens by model, failed runs by error category (each with "show as table"). Tables: model performance, agent history, adaptive routing cells. **Adaptive routing** panel: on/off, minimum samples, maximum adjustment, and a history of decisions where it was applied.

### Knowledge

Search over what the orchestrator has indexed for a project: requirements, ADRs, docs, task summaries, error resolutions, code metadata, skills and model history. Search is lexical (words), explicit (only when you press Search) and every hit shows where it came from. `Index now` indexes changed files (a "full" option re-reads everything and is slower). Statistics show documents, text indexed, last update, skipped files and index errors. Deleting a document removes the index entry only, never the source file. Indexing also happens automatically when a task completes or fails.

### Skills & MCP

The catalogue of skills and MCP servers (41 entries). Filters: search, category, kind, state, and "show activation for project". Opening an entry shows description, source, install method, compatibility, a security report with findings, the skill's `SKILL.md` when downloaded, activations per project and the event history. Actions: Download, Activate, Deactivate, Update, Rollback (section 11).

### Logs

The raw event log with filters (event type prefix such as `task.` or `run.output`, project, task id, agent id, text search). Select an event to see its payload as text. `Pause` freezes the list while you read; `Resume` merges the new events.

### Settings

Policy limits used by the scheduler and supervisor: parallel workers, parallel per project, retry attempts, repair cycles, consecutive failures, run time, heartbeat timeout, quota recheck interval and others. Values are validated and saved to the backend; the defaults come from `config/policies.yaml`. A per-project parallelism above the global worker cap has no effect.

## 4. Task states

| State | Meaning | What to do |
|---|---|---|
| CREATED | Task exists, not started | Press Start (or `orchestrator run <id>`) |
| QUEUED | Waiting for a free worker or for its dependencies | Nothing; it starts by itself |
| PLANNING | The leader is making a plan (skipped if the task has a plan and criteria) | Wait |
| RUNNING | A provider is working in the task's worktree | Watch the Log tab; Pause or Cancel if needed |
| TESTING | The orchestrator is running the verification | Wait |
| REVIEWING | The leader is reviewing the result | Wait |
| WAITING_FOR_QUOTA | A provider reported a rate limit or exhausted quota | Wait: it re-checks every `quota_recheck_seconds` (default 300) and resumes when the provider is usable again or another suitable model exists. See Providers & Usage |
| NEEDS_APPROVAL | A human decision is required | Open Approvals, read the details, approve or deny |
| PAUSED | You paused it | Resume when ready |
| BLOCKED | It cannot continue, for example a dependency failed, no suitable model, authentication error, or an approval was denied | Read the reason on the task (Timeline tab). Fix the cause, then Retry |
| FAILED | Retry limits were reached or verification kept failing | Read the Verification and Log tabs, then Retry (you can change the task first) or Cancel |
| COMPLETED | Verified (or accepted by you as unverifiable) | Review the Diff tab; the changes are on the task's branch `orch/<task id>` |
| CANCELLED | You cancelled it | Terminal |

Retry on FAILED or BLOCKED re-queues the task. Resume applies to PAUSED. The CLI command `orchestrator resume <task>` does the right one of the two.

## 5. Step by step: add a project, create and run a task with verification

Windows verification commands require an owned Git worktree and a working task-specific AppContainer.
Python and standard Program Files Node/npm run from validated private copies. `npm test` receives empty
user/global configuration; `npx` is offline and only uses already-installed local dependencies. Verification
does not install dependencies or inherit your package registry credentials. Unsupported runtime setups or
failed isolation block execution. See [installation security notes](INSTALLATION.md#8-security-notes).

1. Open Mission Control. In the Projects panel press `Add project`. Enter a name and the absolute path of an existing folder, for example `D:\work\my-project`. Optionally add default verify commands, such as `npm test` (one per line). Save.
2. Press `New task`. Fill in:
   - Title and description (what you want done).
   - Acceptance criteria (what must be true when it is done).
   - Verify commands: commands the orchestrator runs in the task's worktree, for example `npm test`. Exit code 0 means pass.
   - Optionally: priority, task type, role, dependencies, preferred provider and model.
   - `Queue immediately after creation` starts it at once; otherwise the task stays CREATED.
3. Open the task and press `Start` if it is CREATED.
4. The task goes QUEUED, (PLANNING), RUNNING, TESTING, (REVIEWING), COMPLETED.
5. Open the Diff tab to see what changed and the Verification tab to see the checks that passed.

If you give no verify command and no acceptance criteria, there is nothing to verify. The task then stops in NEEDS_APPROVAL and can only complete if you approve it as an unverifiable completion.

If the project folder is not a Git repository, `git_init_in_project` approval is required before initialisation.
Tasks remain BLOCKED until approval enables an isolated worktree. After approval, explicitly retry the task;
registration or denied approval never permits execution in your original folder.

## 6. Read the live log

Open the task, tab **Log**. It shows the provider's output as it arrives, tool calls (for example which file was written), state changes and verification results. It scrolls with new output. The same events are in the Logs screen (all tasks) and from the CLI:

```
scripts\orchestrator.cmd logs tsk_xxxxxxxx --follow
```

Secrets that match known formats are replaced with `[REDACTED]` before they are stored.

## 7. Handle an approval and retry a failed task

Approval: when a task is NEEDS_APPROVAL, the top bar's approvals counter turns amber. Open Approvals, read the kind and details, optionally add a note, press `Approve` or `Deny`. Common kinds:

| Kind | Meaning | If you approve |
|---|---|---|
| `unverifiable_completion` | The task has nothing to verify | The task is completed and recorded as `unverifiable_accepted` |
| `git_init_in_project` | The folder is not a git repository | Git is initialised there |
| `permission_escalation` | A read-only run (plan, review, advise) was denied an action | The next run of that phase may edit files inside the task's worktree. It does not grant network access or access outside the worktree. The approval text is specific to the provider and says what that provider's confinement does and does not cover |
| `high_risk_task` | The router classified the task as critical risk | The task runs |
| `untrusted_tool_install` | A skill or MCP server activation | The skill is activated in that project |
| `leader_verify_commands` | The leader proposed verification commands the safety filter refused | Exactly that list becomes the task's verify commands. Read the commands first |
| `leader_clarification`, `leader_escalation`, `leader_budget` | The leader needs input, escalated, or used its call budget | The task is re-queued |

Retry: a FAILED or BLOCKED task has a `Retry` button (CLI: `orchestrator resume <id>`). Retry resumes the stored provider session when the provider supports it. If a task fails repeatedly, read the Verification tab: either the work is wrong, or the verify command itself cannot pass.

## 8. Create an objective and review the leader's plan

1. Mission Control, `Objectives`, `New objective` (or the button on the board). Choose the project and describe the outcome you want, not the steps (up to 20,000 characters). Example: "Add CSV export to the reports page, with tests".
2. Leave "Start the tasks as soon as they are planned" off to review the plan first (the default).
3. The objective shows `planning` while the leader works, then deliverables, prerequisites, acceptance criteria and a graph of tasks with their dependencies. A failed plan shows the reason from the backend.
4. The created tasks are in state CREATED. Open each one to check the title, criteria and verify commands, then start them. Dependencies still decide the order.

If the leader proposes verification that the safety filter does not accept, the task does not receive it automatically: you get a `leader_verify_commands` approval that lists everything proposed.

## 9. Switch the leader, ask the advisor, change routing

Switch the leader (Leadership screen): pick a candidate in the Leader panel and press `Switch leader`. The backend pauses assignment, checkpoints the coordination state, sets the new leader, asks it to restate that state as valid JSON, then resumes. If the new leader fails validation you see the reason and the previous leader stays. Running workers are not stopped. CLI: `scripts\orchestrator.cmd leader set --provider claude --model opus`.

The advisor takes a model call and counts against your budgets (a call may use subscription quota):

- Leadership screen, "Ask the advisor": write the problem, optionally pick a task, add options and constraints. The answer shows the recommended option, risks, disagreements and the evidence that would change the recommendation. It is stored as a decision.
- CLI: `scripts\orchestrator.cmd advisor consult --task tsk_xxxxxxxx --problem "Which migration order is safest?"`

Change routing preferences (Routing screen, Preferences): reorder the provider order, disable a provider or a model. This affects later routing decisions, not tasks already running. Use `Reset` on an item to return to the value from `config/routing.yaml`.

## 10. Read "why this model", knowledge and analytics

**Why this model.** Routing screen, Decisions list, select a decision (or filter by task). Read in this order: Classification (task type, complexity, risk, required capabilities), Ranked candidates with the score breakdown (tier fit, your preference, continuity with the previous run, usage headroom, history), Rejected candidates with the reason (not installed, missing capability, exhausted, below minimum tier, disabled), Fallback (why a switch happened), and whether adaptive routing applied. CLI: `scripts\orchestrator.cmd routing preview --title "Refactor the auth module" --type coding` and `routing decisions`.

**Knowledge.** Select a project, press `Index now`, wait for the counters to update, type words in the search box and press Search. Open a hit to see its source and its text. The results are only what was indexed; they can be outdated. CLI: `scripts\orchestrator.cmd knowledge search "token refresh" --project prj_xxxxxxxx --limit 5`.

**Analytics.** Every figure has a chip with its exactness (section 12).

- **No data yet** (or an empty chart/table): nothing has been recorded for the chosen filters. Run a task first or widen the date range. Missing values are shown as missing, never as 0.
- **Low evidence**: a figure computed from fewer than 5 samples carries the note "Low evidence: fewer than 5 samples. Do not read this as a trend." The sample count `n` is shown next to rates. Adaptive routing is stricter: it only uses a (provider, model, task type) cell once it has the minimum number of verified runs (default 20); the table shows "Needs 20 verified runs before adaptive routing uses it" until then. Until then routing is purely rule-based.

## 11. Skills and MCP servers

Browse: Skills & MCP shows the catalogue. An entry is in one of these states:

| State | Meaning |
|---|---|
| CATALOGUED_ON_DEMAND | Listed only; nothing downloaded |
| DOWNLOADED_AND_DISABLED | Files fetched into the local store; not active in any project |
| INSTALLED_AND_ACTIVE | Active in at least one project |
| NEEDS_SECURITY_REVIEW | Downloaded, but the static inspection found something to review (for example bundled scripts); needs your approval before activation |
| INSTALL_FAILED | The download failed; you can retry |
| INCOMPATIBLE | Not usable on this machine or with these tools |

Some entries cannot be downloaded: those with no known source ("No source URL is known", you can enter one and then it is marked as set by you), documentation-only entries, and entries where the drawer says "Catalogue only". For these the drawer shows the documented instructions to follow yourself.

Steps:

1. Open the entry, read the description, source, security report and `SKILL.md`. Check the source repository yourself.
2. `Download`: fetches the pinned version into `data\skills\` without running anything. Needs confirmation.
3. `Activate`: choose the project. This requires an `untrusted_tool_install` approval. The first press creates the approval and changes nothing. Approve it in Approvals, then press `Activate` again. Skill files are copied into that project's `.claude\skills`, or an MCP entry is added to the project's MCP configuration. Nothing global is touched.
4. `Deactivate`: removes exactly what activation wrote in that project.
5. `Update`: fetches the newest pinned version and keeps the previous one. Activations of the old version are removed; activate again (with a new approval) to use the new version.
6. `Rollback`: returns to the previous version.

Activation for Codex is not supported; use the Claude target. CLI equivalents: `skills list`, `skills show <id>`, `skills install <id>`, `skills activate <id> --project prj_xxxxxxxx`, `skills deactivate <id> --project prj_xxxxxxxx`, `skills update <id>`, `skills rollback <id>`.

Status note: the whole flow has been tested with a local fake source. A real GitHub fetch has not been run yet.

## 12. What the badges and chips mean

State badges are the task states of section 4.

Exactness chips (Providers & Usage, Analytics, task usage):

| Chip | Meaning |
|---|---|
| `exact` / `measured` (green) | Taken directly from provider output or computed from recorded data |
| `estimated` / `ai_evaluated` (amber) | An estimate or a model's judgement. Cost figures are estimates (list-price equivalent), not money charged |
| `unknown` / `missing` (grey) | Not observed. It is shown as unknown, never as zero |

Usage states per provider: AVAILABLE, LOW, EXHAUSTED, COOLDOWN, UNKNOWN, AUTH_ERROR, PROVIDER_ERROR. Authenticated can be "unknown": Antigravity has no way to report its login state, so only a successful call proves it.

## 13. The same workflows from the command line

Use `scripts\orchestrator.cmd` (works in cmd.exe and PowerShell). Add `--json` to any command for raw JSON. The backend must be running (`dashboard` or `serve`). Exit codes: 0 ok, 1 API or usage error, 2 backend not reachable.

```
scripts\orchestrator.cmd doctor                       # local checks, no model call
scripts\orchestrator.cmd status                       # health, scheduler, leader, task counts, approvals
scripts\orchestrator.cmd providers refresh            # re-detect CLIs and models (no model call)
scripts\orchestrator.cmd models --provider claude
scripts\orchestrator.cmd usage

scripts\orchestrator.cmd projects add shop D:\code\shop --verify-cmd "npm test"
scripts\orchestrator.cmd projects list
scripts\orchestrator.cmd projects show prj_xxxxxxxx
scripts\orchestrator.cmd projects remove prj_xxxxxxxx   # registration only

scripts\orchestrator.cmd tasks create --project prj_xxxxxxxx --title "Fix login bug" --description-file spec.md --verify-cmd "npm test" --criteria "login works"
scripts\orchestrator.cmd tasks list --project prj_xxxxxxxx --state FAILED,BLOCKED
scripts\orchestrator.cmd tasks show tsk_xxxxxxxx

scripts\orchestrator.cmd run tsk_xxxxxxxx --follow    # start and stream; exit 0 only if COMPLETED
scripts\orchestrator.cmd run --project prj_xxxxxxxx --title "Add dark mode" --verify-cmd "npm test" --follow
scripts\orchestrator.cmd logs tsk_xxxxxxxx --follow
scripts\orchestrator.cmd pause tsk_xxxxxxxx
scripts\orchestrator.cmd resume tsk_xxxxxxxx          # resumes PAUSED, retries FAILED or BLOCKED
scripts\orchestrator.cmd cancel tsk_xxxxxxxx

scripts\orchestrator.cmd approvals list
scripts\orchestrator.cmd approvals approve apr_xxxxxxxx --note "looks fine"
scripts\orchestrator.cmd approvals deny apr_xxxxxxxx

scripts\orchestrator.cmd routing preview --title "Refactor the auth module" --type coding
scripts\orchestrator.cmd routing decisions
scripts\orchestrator.cmd leader show
scripts\orchestrator.cmd leader set --provider claude --model opus
scripts\orchestrator.cmd advisor consult --task tsk_xxxxxxxx --problem "Which migration order is safest?"
scripts\orchestrator.cmd knowledge search "token refresh" --project prj_xxxxxxxx
scripts\orchestrator.cmd analytics summary
scripts\orchestrator.cmd skills list --kind skill --state installed
```

There is no CLI command to create an objective or to trigger knowledge indexing; use the dashboard for those. (Verified from `orchestrator --help`.)

## 14. Where data lives and how to back it up

The data folder is `%LOCALAPPDATA%\AI-Orchestrator` (outside the repository; see [INSTALLATION.md](INSTALLATION.md) section 3): `orchestrator.db` (projects, tasks, events, settings), `api-token`, `logs\server.log`, `server.pid`, `server.lock`, `skills\`. Task worktrees (one git worktree per task) are in a separate folder, `%LOCALAPPDATA%\AI-Orchestrator-worktrees`. Set `ORCHESTRATOR_DATA_DIR` (and optionally `ORCHESTRATOR_WORKTREES_DIR`) to move them. Deleting the repository does not delete your data.

Backup: stop the server (`scripts\stop.cmd`), then copy the whole data folder (the database uses WAL mode, so copying while the server runs can miss recent writes). Do not publish the copy: it contains the API token and the full event history. To restore, stop the server and put the folder back. The work products are git branches `orch/<task id>` in your own project repositories; those are backed up with the repositories.

After a crash or a stop while tasks were running, the next start reconciles them: runs whose process is gone are marked interrupted, and their tasks go back to QUEUED with a recovery checkpoint, or to BLOCKED when side effects are uncertain.

## 15. Troubleshooting

Setup, ports, tokens, WebSocket close codes 4401/4403, "frontend not built": see [INSTALLATION.md](INSTALLATION.md) section 7.

| Symptom | Likely cause and action |
|---|---|
| Dashboard shows "unauthorised" | No token in this tab. Run `scripts\orchestrator.cmd dashboard` to open a link with the token |
| `orchestrator serve` says another instance already uses the data directory | A server is already running on that data folder. Use it, or run `orchestrator stop` |
| Top bar says `disconnected` or `reconnecting` | The server stopped or restarted. Start it again; the page reconnects |
| A provider shows "not installed" or authentication unknown | Run `providers refresh`. Check that `claude --version`, `codex --version`, `agy --version` work in a normal terminal |
| Task is BLOCKED with "no provider" | No installed, authenticated provider has the required capability. Check Providers & Usage and Routing preferences |
| Task stays WAITING_FOR_QUOTA | The provider reports exhausted or rate-limited. Check Providers & Usage; it re-checks automatically |
| Task FAILED although the agent said it succeeded | Verification failed. Read the Verification tab. This is by design |
| Leader's verify commands are rejected | The safety filter only accepts test, lint and build entry points and a few read-only git commands. Approve the list in Approvals only after reading it, or add verify commands yourself on the task |
| A tiny Codex task used many tokens | Known issue (Codex loading the user's MCP servers); the adapter now disables them by default, but this has not been re-checked on a real run |

## 16. FAQ

**Does it keep working when the PC is off or asleep?** No. It is a local program; nothing runs when the machine or the server is off. Running tasks are recovered at the next start.

**Does it cost extra money?** It starts the provider CLIs you are logged in to, so usage counts against those subscriptions and their limits. No metered API is enabled: the policy allows only the `subscription_cli` and `local` billing classes and sets external API spend to 0. Activating anything else needs an explicit approval.

**Why does usage say Unknown?** The provider CLIs do not expose remaining subscription quota. The orchestrator shows only what they emit (Claude's rate-limit window, per-run token counts, errors). Anything else is shown as unknown rather than guessed.

**Why did a task not complete when the model said it was done?** Completion needs a passing verification, by design. Add acceptance criteria and verify commands to the task.

**Can I use it from another computer or my phone?** No. It binds to `127.0.0.1` only and refuses other addresses. Do not expose the port.

**Which files can agents change?** Execution runs in the task's own Git worktree under `%LOCALAPPDATA%\AI-Orchestrator-worktrees\`, on a branch `orch/<task id>`. Claude workers get file tools only (no shell) by default, so the orchestrator runs verification. Codex is write-confined to the worktree plus the temp folder but can read other files. Windows verification requires a separate task-specific AppContainer, with writes restricted to the task and its scratch/cache areas, and no network capabilities or loopback exemption. See [SECURITY.md](SECURITY.md) for residual risks. Non-Git and legacy in-place projects cannot execute tasks until Git initialisation is approved and worktree isolation is available. Merging a task branch into your main branch remains a manual Git step.

**Is my data sent anywhere?** The orchestrator itself sends nothing to the network. The provider CLIs send prompts to their vendors as they normally do; the skills manager fetches from GitHub only when you press Download.
