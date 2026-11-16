# Roadmap: V5.1 "Hybrid Adaptive Risk-Based Orchestration"

Status: **planned. Nothing in this document is implemented in V5.** It describes direction, not a promise of
scope or dates. Where V5 already provides a building block, that is stated.

## 1. Goal

V5 routes each task with deterministic rules and records why. V5.1 extends this from "pick one model for one task"
to "pick the right amount of structure, models and budget for a project", while keeping the mandatory security
controls (isolation, verification, approvals) identical in every mode.

Principles carried over from V5:

- Decisions are deterministic where possible and always stored with their reasons.
- Anything the provider CLIs do not expose stays "unknown"; no capability, quota or price is invented.
- A task still completes only after recorded verification or an explicit approval.
- Subscription availability and metered API cost are different things and are never merged into one number.

## 2. Components

### 2.1 AI Project Architect

Turns a natural-language project description into a structured, editable plan before any worker starts:

- requirements, architecture outline and milestones;
- a task graph with dependencies;
- acceptance criteria and verification commands per task.

The plan is shown in the dashboard and can be edited. **Nothing launches until the user approves it.**
Building block in V5: the leader can already decompose an objective into tasks with dependencies and propose
verification commands, which pass through a safety filter and an approval.

### 2.2 Model Registry

A registry of what is actually available on this machine, based on observation rather than assumption:

- availability and authentication state per provider and model;
- capabilities (including per-task capability flags);
- observed latency and reliability from recorded runs;
- no invented quota: if a provider does not report a limit, it is shown as unknown.

Building block in V5: provider discovery, `config/models.yaml` tiers and capability flags, usage states, analytics.

### 2.3 Smart Router

Extends the V5 rule-based router with additional inputs, still deterministic and still explained:

- task complexity and risk;
- required capabilities;
- current availability;
- past success for the same (provider, model, task type) once enough verified runs exist;
- observed latency;
- cost class (subscription versus metered).

Building block in V5: tier/role rules, scoring weights in `config/routing.yaml`, bounded adaptive adjustment, stored
routing decisions with candidate scores.

### 2.4 Dynamic Agent Hierarchy

Instead of a fixed single leader, the team structure can be sized to the project:

- optional strategic leader (direction, milestones);
- operational leader (task-level planning and review);
- specialists (implementation, tests, security, research);
- advisors (independent second opinions);
- an escalation manager that decides when a problem moves up a level;
- **bounded delegation**: explicit limits on depth, fan-out and number of calls so hierarchy cannot grow without bound.

Building block in V5: one leader, one advisor, role-based routing, per-task call budgets.

### 2.5 Resource Governor

Protects the machine the orchestrator runs on:

- CPU and RAM awareness;
- concurrency limits derived from measured headroom rather than a fixed number;
- dedicated runners and queues for heavy test suites;
- back-pressure instead of starting work that would exhaust memory.

Building block in V5: global and per-project worker limits in `config/policies.yaml`.

### 2.6 Budget Governor

Tracks consumption and enforces limits:

- tokens, including cached input tokens where the provider reports them;
- number of calls and retries;
- **subscription availability** (can I run this now, will I be rate limited) and **API cost** (money) are tracked
  separately and never converted into each other;
- metered API use stays off unless explicitly approved.

Building block in V5: usage states with exactness labels, retry and leader-call limits, billing classes.

## 3. Operating modes

All three modes keep the same mandatory security controls: worktree isolation, copy-in verification, approvals,
the worker-origin guard and provider confinement rules.

| Mode | Intent | Behaviour (planned) |
|---|---|---|
| `DIRECT` | Predictable and simple | One chosen model per task, no extra hierarchy, no automatic escalation. Closest to V5 behaviour. |
| `SMART ECONOMY` | Lowest consumption that still verifies | Prefers cheaper tiers and fewer calls, escalates only after recorded failures, uses advisors sparingly. |
| `SMART QUALITY` | Highest confidence | Prefers stronger tiers, adds independent review and cross-provider verification for risky tasks, accepts higher consumption. |

## 4. Visual interface extensions

Extensions to Mission Control and the Pixel Office, all driven by recorded events:

- which models are actually active right now;
- delegations between agents;
- worktree identity (task, branch, path) per active agent;
- CPU/RAM and queue usage;
- the selection reason for each model choice, next to the task;
- a clear visual difference between idle animation and real work (no animation implies activity that did not happen).

## 5. Efficiency benchmarking methodology

Claims about efficiency need a method before they need numbers. Planned approach:

1. Fixed benchmark task set with objective acceptance checks (tests, structured file checks).
2. Same tasks run in `DIRECT`, `SMART ECONOMY` and `SMART QUALITY`, repeated enough times to report spread.
3. Measure only recorded values: verified completion rate, retries and repair cycles, tokens (including cached),
   calls, wall-clock and active time. Mark anything estimated as estimated.
4. Report subscription impact and metered cost separately, with provider and version recorded.
5. Publish the method and the raw per-run records together with any summary; no single headline number.

## 6. Distribution

Two options are planned. Neither exists yet.

### Option A: source distribution

Clone the repository and run `scripts\start.cmd`, as in V5. Requires uv, Node.js and Git on the machine. This stays
the reference path and the one used by contributors.

### Option B: easy-install Windows release

A packaged installer or portable bundle for people who do not want a development toolchain:

- prebuilt dashboard, so Node.js is not needed to run it;
- bundled or managed Python runtime;
- code signing if a signing certificate can be obtained;
- a guided first-run check that detects the provider CLIs, shows whether each one is logged in, and runs the
  `doctor` checks;
- third-party license notices shipped with the bundle.

Open questions: how verification runtimes and the Codex activation attestation are provisioned in a packaged layout,
and how updates are delivered without weakening the isolation checks.
