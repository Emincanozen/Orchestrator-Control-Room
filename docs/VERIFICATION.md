# Verification evidence (V5.0.0)

This page summarises how the V5 release was verified. Every number below comes from a real run of the
released source on Windows 11; nothing is estimated. Mock providers are the default in the automated suites;
real-provider evidence is listed separately and is deliberately small.

## Automated suites on the release source

| Suite | Command | Result |
|---|---|---|
| Backend (complete, non-smoke) | `cd backend; uv run pytest -q` | **1855 passed, 0 failed, 6 skipped** (30.5 min, one worker) |
| Frontend unit | `cd frontend; npm.cmd run test` | **197 passed** (21 files) |
| Frontend lint / types | `npm.cmd run lint` (`tsc --noEmit`) | **pass** |
| Frontend production build | `npm.cmd run build` | **pass** (one existing >500 kB chunk-size warning) |
| End-to-end (Playwright, real backend, mock providers) | `npm.cmd run e2e` | **33 passed**, 3 optional screenshot specs skipped |

The backend run included the opt-in native Codex containment tests (`ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT`).
The 6 skips, with their recorded reasons:

- 4 historical "in-place" live-alias probes whose premise no longer exists: verification commands never run
  in the task worktree any more (copy-in, see below), so a command cannot alias the worktree.
- 1 POSIX-only fail-closed case (this is a Windows run).
- 1 ACL fixture that needs a volume granting a broad principal (not present on the test volume).

`-m smoke` (3 tiny real-CLI tests) is opt-in and not part of the counts above.

## Clean-clone install test

The published tree itself was cloned into a new, empty directory and installed and tested there, unmodified,
exactly as the README describes:

| Step | Result |
|---|---|
| `scripts\start.cmd` (first run: virtualenv, dashboard build, server start, browser opens) | **pass**, server ready in 51–58 s; version 5.0.0; `orchestrator doctor` OK; `scripts\stop.cmd` stops only its own server |
| Backend `uv run pytest -q` | **1808 passed, 0 failed, 18 skipped** (9 min 49 s) |
| Frontend `npm.cmd ci`, `npm.cmd run lint`, `npx.cmd vitest run`, `npm.cmd run build` | **pass**: 197 tests passed (21 files); lint and build pass |

The clean clone skips 12 more backend tests than the development run: the opt-in native Codex containment
tests, which need an explicit private non-TEMP data root (`ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT`) and therefore
skip by design in a fresh checkout. The other 6 skips are the ones listed above.

## Acceptance criteria

The product requirements in [PRD.md](PRD.md) define **58 mandatory acceptance criteria**; all 58 are met.
The last three closed in this release:

- **DL-01** first launch: `scripts\start.cmd` prepares the environment on first run (virtualenv, dashboard
  build), starts the server and opens the dashboard in the default browser — observed directly on Windows.
- **ER-19** per-phase permissions enforced by each provider's own mechanism, and **ER-20** Codex runs never
  load the user's own Codex plugins or MCP servers — both shown with real provider runs (next section).

## Real provider runs

Each run is one tiny task through the full pipeline
(`CREATED → QUEUED → RUNNING → TESTING → COMPLETED`), verified by structured file checks and a command
(`pytest` or `python -c`) executed in the copy-in AppContainer sandbox.

| Provider (CLI) | Model | What was observed |
|---|---|---|
| Claude Code | claude-sonnet-5-5 | `--restricted` file tools only; a single `Write` inside the task worktree; no shell |
| Codex CLI | gpt-5.6-sol | owned runtime and MXC profile `orch-write`; a single in-worktree `file_change`; **0 MCP calls** although the user's own Codex config defines MCP servers and plugins |
| Antigravity CLI | gemini-4-hydrogen-medium | task completed; read scope and workspace-config loading are not determined (documented limitation) |

The screenshots and the demo GIF in the README come from a disposable demo project where these providers
completed real tasks (`low_stock()` implementation, `pytest` edge-case tests, README, CHANGELOG,
CONTRIBUTING), each verified by the sandboxed `pytest` run or a structured file check.

## Codex activation attestation

Codex execution is refused until `orchestrator codex-attest` records a passing attestation for the exact
installed Codex package and the exact isolation code. The 12 model-free native checks, each with a positive
control under identical syntax and location:

1. sandbox positive control (startup, in-task write and read)
2. owned Codex home: read denied · 3. write denied
4. sibling worktree: read denied · 5. write denied
6. read-only profile: write denied
7. untrusted project `.codex/config.toml` MCP server does not start (a trusted control does start)
8. pre-planted hardlink refused before credential preparation
9. hardlinked Git metadata file refused · 10. hardlinked Git metadata directory refused
11. worker-created hardlink to an outside file gives no read/write
12. worker-created junction to an outside or sibling directory gives no read/write

A denial counts only for a process that completed; a timeout is a harness failure, never a pass. Observed on
Codex CLI 0.169.0: 12/12 PASS (the MXC profile denies link creation entirely, even inside the task).

## Isolation design that the tests exercise

- Verification commands run in a **per-command AppContainer** (zero capabilities, no loopback exemption)
  with a Low-integrity token, a private desktop and nested Job Objects, inside a **disposable copy** of the
  worktree that is granted while still empty and filled through no-follow handles. Nothing is copied back;
  the copy is removed through handles after the job drains, and undeletable residue is quarantined.
- The private Python/Node runtimes are pinned and fully validated once per process (prewarmed at server
  start) and re-checked cheaply on every command.
- Every cleanup uncertainty fails closed: unverified process exits keep the session, runtime reference and
  run directory, and the affected worktree is quarantined.

## Known limitations

See [SECURITY.md](SECURITY.md) for the full list. In short: single-user local tool; standard AppContainer
broker channels and locations readable by all application packages remain; Antigravity's confinement is not
determined; Codex network reachability inside its profile is not asserted; verification commands do not
share files with each other (a build in one command is not visible to the next).
