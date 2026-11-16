# Test Plan

This document describes how the project is tested, how to run each level, and which acceptance criteria from [PRD.md](PRD.md) are covered by which tests. It is a plan and an inventory, not a test report: it does not say that the tests pass. Results and verification dates are in [VERIFICATION.md](VERIFICATION.md). The inventory below was taken by reading the test files in the repository; test names are cited as they appear in the code.

## 1. Strategy

- Deterministic behaviour (state machine, scheduler, routing, recovery, verification, security checks) is tested with the scripted mock provider and fake adapters, so it needs no network and spends no quota.
- Real provider CLIs are used in a small number of opt-in checks, each a tiny call (see section 5). Real adapter checks beyond those smoke tests were done by hand and are summarised in [VERIFICATION.md](VERIFICATION.md); they are not repeatable automated tests.
- The dashboard is tested at three levels: Vitest unit and component tests, Playwright end-to-end against the real backend with the mock provider, and manual or screenshot inspection.
- A feature is only called done when it has a passing test or observed evidence.

## 2. Test levels and how to run them

All backend commands run in `backend/`. The default pytest configuration excludes tests marked `smoke`
(`pyproject.toml`: `addopts = "-m 'not smoke'"`). `tests/conftest.py` disables automatic real-provider
registration. Explicit native security fixtures can run installed Python/Node and Codex metadata commands
against fresh fixture configuration; they do not call a model. The owned-runtime late-project-MCP fixture
starts only its own synthetic server as an explicit positive control; original user MCP configuration is
not copied or loaded by that fixture. Real model calls remain
opt-in. Windows AppContainer tests require a normal, non-elevated Windows user token, rather than an extra
restricted tool token. Use a fresh `--basetemp` under Windows TEMP, outside this installation and state roots.

Native security coverage includes private Python/Node copy integrity and source ACL preservation, npm success
and failing-script exit codes, local offline npx, hostile project module junctions, credential/cross-task/outside
file denial, IPv4/IPv6 TCP/UDP denial and descendant job drain. Codex fixtures exercise effective project/user
MCP metadata and fail-closed inventory errors. Metadata alone is not real exec/resume isolation evidence.

| Level | Location | Purpose | Command |
|---|---|---|---|
| Unit | `backend/tests/unit/` | State machine, DB schema and transactions, command splitting, supervisor mapping, activity mapping, task service | `uv run pytest -q tests/unit` |
| Contract | `backend/tests/contract/` | Provider adapters against recorded JSONL fixtures (`tests/fixtures/providers/`), process runner, registry, usage logic, Codex isolation, verified regressions | `uv run pytest -q tests/contract` |
| Integration | `backend/tests/integration/` | Full pipeline over the app with the mock provider, scheduler, HTTP API, WebSocket replay, default app wiring | `uv run pytest -q tests/integration` |
| Recovery | `backend/tests/recovery/` | Restart and crash reconciliation | `uv run pytest -q tests/recovery` |
| Security | `backend/tests/security/` | API, filesystem/Git races, AppContainer, private runtimes, worker lifetime and provider isolation metadata | `uv run pytest -q tests/security` |
| Router and usage | `backend/tests/router/`, `backend/tests/m2b/` | Classifier, filters, scoring, fallback, adaptive adjustment, routing flow, usage and quota, providers/models/usage/routing API | `uv run pytest -q tests/router tests/m2b` |
| Leader and advisor | `backend/tests/leader/` | Single-leader invariant, plan, review, objectives, structured checks and safety filter, advisor, switch protocol, API and seat activity | `uv run pytest -q tests/leader` |
| Skills | `backend/tests/skills/` | Catalogue import, fetch without execution, activation, deactivation, update and rollback, API | `uv run pytest -q tests/skills` |
| Knowledge | `backend/tests/knowledge/` | Indexing, retrieval, labelling, API | `uv run pytest -q tests/knowledge` |
| Analytics | `backend/tests/analytics/` | Metrics, exactness, adaptive settings, API | `uv run pytest -q tests/analytics` |
| CLI | `backend/tests/cli/` | Commands against a live app, exit codes, token handling, serve/dashboard/stop/doctor, follow | `uv run pytest -q tests/cli` |
| Backend, everything non-smoke | all of the above | Regression | `uv run pytest -q` (latest complete Windows run about 12 minutes) |
| Frontend type check | `frontend/` | TypeScript | `npm.cmd run lint` (runs `tsc --noEmit`) |
| Frontend unit | `frontend/src/**/*.test.ts(x)` | API client, store reducer, WebSocket client, state-transition mirror, Office engine and canvas, per-view component tests | `npm.cmd run test` |
| Playwright e2e | `frontend/e2e/*.spec.ts` | Dashboard against the real backend with the mock provider | `npm.cmd run e2e` (builds first) or `npx.cmd playwright test` (needs `frontend/dist`); first time `npx.cmd playwright install chromium` |
| Screenshots | `frontend/e2e/screenshots.spec.ts` | Regenerates `docs/screenshots/` (skipped unless `SCREENSHOTS=1`) | PowerShell: `$env:SCREENSHOTS=1; npx.cmd playwright test screenshots` |
| Real-CLI smoke (opt-in) | `backend/tests/smoke/test_real_providers.py` | One tiny read-only call per installed provider CLI | `uv run pytest -q -m smoke tests/smoke` |

In PowerShell use `npm.cmd` and `npx.cmd`. Playwright runs with one worker and a single shared backend; the port is `E2E_PORT` (default 8811).

## 3. The mock provider and how e2e scripts it

`backend/orchestrator/providers/mock.py` is a scriptable provider that never spawns a process and never spends quota. It is registered only when `ORCHESTRATOR_ENABLE_MOCK=1`. Each `execute` call consumes one script: a FIFO queue, then an optional scripter callback, then a default script (success). Steps include session, text, tool use, usage, rate limit, writing a file, sleeping, succeeding, failing with an error category, hanging until cancelled, crashing, and raising (to simulate an adapter bug).

Backend tests build their apps with the mock or with fake adapters in the `conftest.py` files of each suite. The Playwright run starts the real app through `frontend/e2e/support/mock_server.py` on a temporary data directory (`global-setup.ts`), with only the mock provider registered. That script keys behaviour on markers in the task text:

| Marker in title or description | Behaviour |
|---|---|
| `MOCK:HANG` | works until the task is cancelled |
| `MOCK:FIXLATER` | first run misses `fix.txt` (verification fails), the repair run writes it |
| `MOCK:SLOW` | emits output over about 3 seconds, writes `result.txt`, then succeeds |
| (none) | writes `result.txt` and succeeds immediately |

`global-setup.ts` also adds the e2e origin to `allowed_origins`, pins the leader to the mock provider and disables two extra fake providers (`zeta`, `zulu`) that the leadership switch tests use.

## 4. Fixtures and recorded data

`backend/tests/fixtures/providers/` holds recorded JSONL streams (`claude_success`, `claude_quota`, `codex_success`, `codex_quota`, `agy_success`, `agy_error`, `agy_denied`) used by the contract tests. Recorded streams reflect the CLI versions noted in `config/models.yaml`; they must be re-recorded if a CLI changes its output format.

## 5. Policy on real provider calls

- Never in the default run. Real calls exist only in the `smoke` marker and in manual checks.
- Opt-in and tiny: one short prompt ("Reply with exactly: X_OK") per provider, read-only, in a temporary directory, with the cheapest known model (`haiku` for Claude, `gemini-3.8-flash-low` for Antigravity, the configured default for Codex). The test is skipped when the CLI is not installed and fails (not skips) if a provider reports a usage limit.
- Manual real checks (write path, resume, full pipeline, leader calls) are done as single cheap calls in a temporary git repository, recorded with the date in [VERIFICATION.md](VERIFICATION.md), and never added to the default suite.
- No metered API is called by any test. Tests for skills use local fake sources; no test fetches from GitHub.
- Never launch a provider CLI from a test without the recursion guard and an explicit tool allowlist.

## 6. Traceability

Criterion IDs come from [PRD.md](PRD.md) section 12. Test names are given without the `test_` prefix. Column "Coverage" uses three values:

- **Automated**: covered by an automated test in the repository.
- **Real-provider check**: observed with a real CLI by a manual or opt-in check (alone or in addition to automated tests).
- **Not covered**: no automated test and no recorded real check.

A coverage entry says that a test exists for the behaviour, not that it passes; see [VERIFICATION.md](VERIFICATION.md) for results. "Partial" means the cited tests cover part of the sentence.

### Interface

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| UI-01 | `frontend/e2e/dashboard.spec.ts` (without a token the dashboard shows the unauthorised screen; a wrong token is rejected) | n/a | Automated |
| UI-02 | `dashboard.spec.ts` (with the token the board opens, the token leaves the address bar and the socket goes live); `frontend/src/api/client.test.ts` (stores `?token=` in sessionStorage and strips it) | n/a | Automated |
| UI-03 | `dashboard.spec.ts` (add a project through the UI, then create, start and watch a task complete) | n/a (a real task was driven through the API, not the UI) | Automated |
| UI-04 | `frontend/src/lib/transitions.test.ts` (covers all 13 states, every allowed action is a legal transition); `frontend/src/views/mission/mission.test.tsx` | n/a | Automated |
| UI-05 | `dashboard.spec.ts` (a page reload restores everything; after the backend restarts the socket reconnects); `frontend/src/store/stream.test.ts`, `reducer.test.ts`; `backend/tests/integration/test_api.py::ws_replay_by_seq_then_live` | n/a | Automated |
| UI-06 | `frontend/src/office/engine/engine.test.ts`, `frontend/src/office/OfficeCanvas.test.tsx`; backend seat events: `backend/tests/leader/test_leader_api_seats.py::leader_and_advisor_seats_show_work_only_while_the_run_is_live`, `backend/tests/unit/test_core_units.py::activity_mapping`, `working_activity_requires_live_process` | Seat events from a real run recorded (see VERIFICATION.md). Watching the Office in a browser during a real run: not done | Automated (partial); the "no network or model call by rendering" part has no dedicated test |
| UI-07 | Component tests per view: `views/mission/mission.test.tsx`, `objectives.test.tsx`, `ApprovalsView.test.tsx`, `leadership/leadership.test.tsx`, `providers/providers.test.tsx`, `routing/routing.test.tsx`, `analytics/analytics.test.tsx`, `knowledge/knowledge.test.tsx`, `skills/skills.test.tsx`, `logs/logs.test.tsx`, `SettingsView.test.tsx`; e2e `dashboard.spec.ts`, `m4c.spec.ts`, `m4d.spec.ts`. The Office has component tests only (no Playwright spec opens it) | n/a | Automated (partial: Office not covered by e2e) |
| UI-08 | `frontend/src/views/analytics/analytics.test.tsx`, `providers/providers.test.tsx`; `m4d.spec.ts` (analytics shows measured values); backend `tests/analytics/test_analytics_metrics.py::empty_db_everything_missing`, `tests/contract/test_usage.py::no_utilization_means_no_fabricated_percentage` | n/a | Automated |

### AI orchestration

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| AI-01 | `tests/contract/test_registry.py` (`refresh_caches_status_and_models_and_survives_failures`, `parse_agy_models_output`), `tests/m2b/test_api_m2b.py::providers_shape_and_refresh` | 25 models discovered for real (see VERIFICATION.md) | Automated + Real-provider check |
| AI-02 | `tests/contract/test_provider_contract.py` (`prompt_never_in_argv`, `dangerous_flags_never_used`, `agy_stdin_message_shape`, `permission_mapping_*`) | Prompt on stdin verified for all three CLIs (ADR-005) | Automated + Real-provider check |
| AI-03 | `tests/router/test_router_routing.py`, `test_router_classifier.py`, `tests/m2b/test_routing_flow.py::routes_by_rules_and_persists_full_decision`; `frontend/e2e/m4c.spec.ts` (preview explains the decision) | The real full-pipeline run recorded its decision (see VERIFICATION.md) | Automated + Real-provider check |
| AI-04 | `tests/router/test_router_fallback.py` (`quota_switches_to_other_provider`, `quota_waits_when_no_alternative`, `auth_switches_else_blocks`, `never_downgrades_tier`), `tests/m2b/test_routing_flow.py::quota_error_switches_provider_with_handoff_checkpoint` | Never triggered by a real quota error (see VERIFICATION.md) | Automated (fake adapters only) |
| AI-05 | `tests/leader/test_leader_invariants.py::database_enforces_a_single_leader_row`, `concurrent_switches_leave_exactly_one_leader`; `tests/leader/test_leader_switch.py::validation_mismatch_rolls_back_to_the_previous_leader`; `m4d.spec.ts` (safe switch; a leader that fails validation is rolled back) | One real switch validation (see VERIFICATION.md) | Automated + Real-provider check |
| AI-06 | `tests/leader/test_leader_objectives.py` (`decomposition_creates_tasks_in_created_state_with_dependencies`, `invalid_decompositions_are_rejected_after_one_repair`); `m4d.spec.ts` (new objective, failed plan) | One real decomposition (see VERIFICATION.md) | Automated + Real-provider check |
| AI-07 | `tests/leader/test_leader_plan_review.py` (`review_approve_still_needs_passing_verification`, `completion_guard_ignores_a_leader_approval_without_verification`); read-only phases: `tests/m2b/test_routing_flow.py::plan_phase_is_read_only` | Leader plan and review inside a real task pipeline: not done (see VERIFICATION.md) | Automated (no real pipeline check) |
| AI-08 | `tests/leader/test_leader_advisor.py` (`advisor_prompt_contains_only_problem_requirements_evidence_options_constraints`, budgets, policies, `decision_records_evidence_status`); `m4d.spec.ts` (ask advisor) | Advisor consult with a real provider: not done (fake adapters only) | Automated (fake adapters only) |
| AI-09 | `tests/m2b/test_routing_flow.py::quota_error_switches_provider_with_handoff_checkpoint`, `tests/integration/test_pipeline.py`; handoff content is built in `checkpoints/handoff.py` | Cross-provider handoff with real providers: not exercised (see VERIFICATION.md) | Automated (partial: no test that inspects the handoff fields as listed in the criterion beyond the checkpoint's creation) |

### Execution reliability

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| ER-01 | `tests/unit/test_state_machine.py` (`assert_transition_matches_table`, `every_pair_through_transition_function`), `tests/unit/test_task_service.py::transition_and_event_are_atomic` | n/a | Automated |
| ER-02 | `tests/integration/test_pipeline.py` (`model_claims_success_without_verification_pass_is_not_completed`, `no_verification_needs_approval_then_accept`), `tests/unit/test_state_machine.py::completed_requires_passing_verification`; e2e `dashboard.spec.ts` (approval path) | One real task COMPLETED with file verification (see VERIFICATION.md) | Automated + Real-provider check |
| ER-03 | `tests/integration/test_pipeline.py::verification_failure_repair_loop_is_bounded`, `repair_succeeds_on_second_attempt`; `dashboard.spec.ts` (a failing verification ends FAILED, can be retried) | n/a | Automated |
| ER-04 | `tests/integration/test_pipeline.py::success_creates_worktree_and_completes`; `tests/integration/test_scheduler.py` (`global_concurrency_limit`, `per_project_limit`, `same_worktree_exclusion_in_place_project`, `dependency_ordering`) | Worktree created in the real run (see VERIFICATION.md) | Automated + Real-provider check |
| ER-05 | `tests/recovery/test_recovery.py` (`restart_requeues_interrupted_run_without_duplicate`, `in_place_crash_blocks_task`, `alive_orphan_is_killed`); `tests/integration/test_scheduler.py::graceful_shutdown_requeues_running_task` | n/a | Automated |
| ER-06 | `tests/contract/test_process.py` (`terminate_tree_kills_child_and_grandchild`, `adapter_cancel_real_process_tree`), `tests/integration/test_scheduler.py::cancel_kills_hanging_run`; `dashboard.spec.ts` (a hanging task can be cancelled) | n/a | Automated |
| ER-07 | `tests/integration/test_scheduler.py` (`quota_error_waits_then_resumes`, `rate_limit_reset_time_used`), `tests/m2b/test_usage_quota.py::quota_check_resumes_only_when_state_changes`, `tests/router/test_router_fallback.py::unknown_is_not_treated_as_quota`, `tests/contract/test_provider_contract.py::not_everything_is_quota` | Not triggered by a real quota error | Automated |
| ER-08 | `tests/integration/test_scheduler.py::transient_errors_retry_then_fail`, `tests/unit/test_core_units.py::supervisor_retry_limits`; `frontend/src/lib/transitions.test.ts` (retry only from FAILED and BLOCKED) | n/a | Automated |
| ER-09 | `tests/m2b/test_routing_flow.py` (`retry_resumes_stored_session_when_adapter_supports_it`, `plain_adapter_never_gets_a_resume_id`) | Codex resume verified for real on 2026-11-11 (see VERIFICATION.md). Resume with a workspace-write override and from another cwd: unverified | Automated + Real-provider check (Codex only) |
| ER-10 | `tests/security/test_approvals.py` (`approve_requeues`, `deny_blocks_with_reason`, `double_resolve_rejected`), `tests/integration/test_api.py::approvals_api`; `dashboard.spec.ts` (an approval appears and can be resolved) | n/a | Automated |
| ER-11 | n/a | One real task COMPLETED through the full pipeline with Codex on 2026-11-12 (see VERIFICATION.md); driven through the API with a temporary data directory, not repeatable as a test | Real-provider check (one run) |
| ER-12 | `tests/security/test_api_security.py` (`token_required`, `health_is_public`, `ws_requires_token`); `tests/m2b/test_api_m2b.py::endpoints_require_the_token`; `tests/skills/test_skills_api.py::auth_required_on_every_skills_route`; similar `auth_required` tests for analytics and knowledge | n/a | Automated |
| ER-13 | `tests/security/test_api_security.py` (`bad_host_rejected`, `origin_checks`, `ws_rejects_bad_origin`); `frontend/src/store/stream.test.ts` (4403 handling) | n/a | Automated |
| ER-14 | `tests/cli/test_cli_core.py` (`serve_refuses_non_loopback_without_the_flag`, `serve_defaults_and_explicit_exposure`) | n/a | Automated (CLI guard only; no test that the default app binds loopback when started another way) |
| ER-15 | `tests/security/test_paths.py` (all cases), `tests/security/test_api_security.py` (`artifact_path_traversal_rejected`, `project_path_validation`) | n/a | Automated |
| ER-16 | `tests/security/test_redact.py`, `tests/security/test_token_leak.py`, `tests/knowledge/test_knowledge_indexing.py::secrets_redacted_before_storage` | n/a | Automated |
| ER-17 | `tests/contract/test_process.py` (`build_child_env_filters_secrets_and_sets_depth`, `secret_does_not_reach_real_child`, `depth_increments_and_guard`), `tests/contract/test_provider_contract.py` (`recursion_guard_refuses_nested_start`, `child_env_is_allowlisted_and_depth_set`) | n/a | Automated |
| ER-18 | `tests/leader/test_leader_plan_review.py` (`safety_filter_rules`, `unsafe_proposed_verify_command_requests_approval_and_is_never_run`), `tests/leader/test_structured_checks.py` (`safety_filter_table_commands`, `safety_filter_structured_checks`, `unsafe_check_in_a_plan_requests_approval_listing_everything_proposed`), `tests/leader/test_leader_objectives.py::unsafe_verify_commands_are_not_attached`. The `.cmd`/`.bat` metacharacter refusal in `scheduler/verification.py` has no dedicated test that was found | n/a | Automated (partial) |
| ER-19 | `tests/contract/test_provider_contract.py` (`permission_mapping_read_only`, `permission_mapping_workspace_write`), `tests/m2b/test_routing_flow.py` (`plan_phase_is_read_only`, `worker_request_carries_permission_cwd_timeout_and_brief`, `read_only_phase_escalation_grants_workspace_write_for_that_phase`) | Real read and write paths on claude, codex, agy (see VERIFICATION.md). Antigravity shell commands are auto-denied headless | Automated + Real-provider check |
| ER-20 | `tests/contract/test_codex_isolation.py` (argument lists, discovery of user MCP servers) | Effect on a real Codex run (token count, no user MCP servers loaded): not verified | Automated (argument level only) |

### Intelligence

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| IN-01 | `tests/contract/test_usage.py` (all), `tests/m2b/test_api_m2b.py` (`usage_reflects_events_with_exactness`, `usage_lists_provider_without_data_as_unknown`) | Real token counts stored; unknown stays UNKNOWN (see VERIFICATION.md) | Automated + Real-provider check |
| IN-02 | `tests/analytics/test_analytics_metrics.py` (`empty_db_everything_missing`, `summary_hand_computed`, `small_n_is_reported`), `tests/analytics/test_analytics_api.py` | Analytics reflected the real run (see VERIFICATION.md) | Automated + Real-provider check |
| IN-03 | `tests/router/test_router_fallback.py` (`adaptive_below_threshold_is_zero`, `adaptive_at_threshold_bounded_with_explanation`), `tests/analytics/test_analytics_api.py` (`adaptive_below_threshold`, `adaptive_above_threshold_and_settings`) | No real evidence exists (needs 20 verified runs per cell) | Automated |
| IN-04 | `tests/knowledge/test_knowledge_indexing.py` (`incremental_unchanged_changed_deleted`, `kinds_and_provenance`, `secrets_redacted_before_storage`, `source_scan_no_writes_outside_knowledge_tables`); `m4d.spec.ts` (index a temp project, find a phrase with provenance) | n/a | Automated |
| IN-05 | `tests/knowledge/test_knowledge_retrieval.py::context_for_task_budget_labels_and_relevance`, `fts_query_injection_safety` | n/a | Automated |
| IN-06 | none found that asserts "no model call on navigation". Related: `backend/tests/security/test_api_security.py` runs with real providers disabled | n/a | Not covered |
| IN-07 | `tests/m2b/test_api_m2b.py::routing_decisions_and_preview`; `frontend/src/views/routing/routing.test.tsx`; `m4c.spec.ts` (a finished task has an explained decision) | n/a | Automated |
| IN-08 | `tests/router/test_router_routing.py` (`filter_billing_class`, `registry_unknown_provider_billing_not_allowed`), `tests/leader/test_leader_invariants.py::default_leader_is_deterministic_verified_and_never_metered`, `no_default_when_only_metered_or_unverified_models_exist` | n/a | Automated |

### Skills

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| SK-01 | `tests/skills/test_skills_catalog.py` (`real_catalog_import`, `reimport_is_idempotent_and_preserves_local_state`) | n/a | Automated |
| SK-02 | `tests/skills/test_skills_install.py` (`repo_content_is_never_executed`, `fetch_uses_protective_git_config`, `download_pins_commit_and_writes_event`) | A real GitHub fetch has never been run | Automated (local sources only) |
| SK-03 | `tests/skills/test_skills_install.py` (`symlink_entry_rejected`, `unsafe_paths_rejected`, `traversal_tree_entry_never_reaches_disk`, `size_and_file_count_limits`) | n/a | Automated |
| SK-04 | `tests/skills/test_skills_activation.py` (`activate_without_approval_returns_202_and_changes_nothing`, `denied_approval_does_not_activate_and_new_request_is_made`); `frontend/e2e/m4c.spec.ts` (download, activate needs approval, approve, activate) | n/a | Automated |
| SK-05 | `tests/skills/test_skills_activation.py` (`deactivate_removes_exactly_what_was_written`, `mcp_entry_is_merged_into_project_mcp_json_and_removed`, `mcp_entry_modified_by_user_is_left_on_deactivate`, `refuses_to_overwrite_foreign_folder`) | n/a | Automated |
| SK-06 | `tests/skills/test_skills_activation.py::update_then_rollback_restores_previous_ref`, `rollback_skips_projects_without_prior_approval`; `tests/skills/test_skills_api.py::mcp_over_http_and_update_rollback` | n/a | Automated |
| SK-07 | `tests/skills/test_skills_api.py::catalogue_only_error_has_instructions`, `tests/skills/test_skills_activation.py::catalogue_only_kinds_return_instructions`; `m4c.spec.ts` (an entry with no source cannot be downloaded) | n/a | Automated |
| SK-08 | `tests/skills/test_skills_activation.py::codex_activation_is_unsupported_by_default` | n/a | Automated |

### Delivery

| ID | Automated tests | Real-provider check | Coverage |
|---|---|---|---|
| DL-01 | `tests/cli/test_cli_core.py::dashboard_starts_a_detached_server_when_not_running` covers the `dashboard` command. `scripts\start.cmd` itself (uv/Node checks, first-run build) has no automated test; a fresh-start walkthrough (not recorded) is not recorded | n/a | Not covered (start.cmd); Automated for `dashboard` |
| DL-02 | `tests/cli/test_cli_core.py::doctor_reports_checks_and_fixes` and the following doctor test | n/a | Automated |
| DL-03 | `tests/cli/test_cli_commands.py` (status, providers/models/usage, projects, tasks, routing, leader, skills, approvals, run, pause/resume/cancel), `tests/cli/test_cli_follow.py`, `tests/cli/test_cli_core.py` (`backend_unreachable_exits_2_with_hint`, `usage_errors_exit_1`, `json_flag_global_and_local_are_valid_json`). Commands `advisor consult`, `knowledge search`, `analytics summary` are covered by `leader_advisor_knowledge_analytics_requests` (request level) | n/a | Automated |
| DL-04 | `tests/cli/test_cli_core.py` (`stop_terminates_only_a_verified_orchestrator_serve_process`, `stop_refuses_a_foreign_process_and_leaves_it_alive`, `stop_ignores_a_recycled_pid`, `stop_with_dead_pid_removes_the_stale_file`) | n/a | Automated |
| DL-05 | Manual review only. No test checks that documented commands match the CLI. The command lines in USER_GUIDE.md were compared with `orchestrator --help` output when written | n/a | Not covered |

## 7. Known gaps

- No Playwright spec opens the Office screen; the Office is covered by Vitest only, and has not been watched in a browser during a real run.
- Nothing asserts that navigating the dashboard causes no model call (IN-06). The design rule (no LLM call from rendering, polling or navigation) is architectural.
- Real provider fallback, cross-provider handoff, leader plan and review inside a real task, advisor with a real provider, and a real GitHub skill fetch have not been run.
- The effect of the Codex isolation arguments on a real run has not been measured.
- The `.cmd`/`.bat` verification path through `cmd.exe` has no dedicated test.
- `scripts\start.cmd` and `scripts\stop.cmd` have no automated test.
- Frontend e2e uses the mock provider only; no e2e run spends real quota.
- The backend suite takes about 7 minutes; running it concurrently with other work is slow on a 16 GB machine.
- Playwright counts differ between commits; re-run the suite on the current tree before relying on any recorded number.


## Finalization regressions (2026-11-14)

- `tests/security/test_required_worktree.py`: non-Git pending/denied approval, legacy Git root/path
  migration, approved explicit retry and persisted recovery; no source-checkout provider execution.
- `tests/security/test_recovery_pid_safety.py`: safe own-process PID reuse controls, inaccessible PID,
  durable restart/retry barrier and retirement, wrong-task and junction rejection. Recovery never kills
  on persisted PID guesses. Real kernel controls are confined to processes created by the test itself.
- `tests/security/test_verification_native_lifetime.py`: pure fake native operations, no OS process
  termination; event-loop heartbeat, repeated cancellation during prepare/runtime/integrity/resume,
  late result adoption, job drain before runtime release, and unknown-exit quarantine. Gate the fake
  resume itself so cancellation cannot silently wait on a native lock in the event-loop thread.
- Real provider evidence is separate from these default tests. Text-only Codex exec/resume and the
  controlled enabled/disabled fake MCP startup experiment do not prove coding filesystem confinement
  or immutable original CLI configuration. Failed coding attempts remain recorded, not converted to PASS.
- Full backend attempts interrupted for a discovered defect are not complete regression results.
  Run targeted checks first, then full security/backend, frontend lint/unit/build and one-worker Playwright
  sequentially. Attribute results to the tested source/commit, not a later unrelated tree.


Native job-origin tests must check actual job membership immediately before real resume, rather than
assuming an OS-visible newly created suspended process is already assigned. For native manual-retry UI
coverage, isolate automatic repair budget to zero in the private fixture and restore the previous effective
policy in finally; require two FAILED transitions. Automatic repair bounds remain separate backend coverage.
When a fail-fast full invocation stops on a corrected fixture, complementary coverage is acceptable only
with exact case-ID union, zero missing cases, unchanged product sources and the failed attempt retained;
label it split coverage, never a single clean full invocation.


## Verification latency diagnostics (V5 remediation)

Enable INFO logging for `orchestrator.scheduler.verification` during a bounded native probe. Each command
produces one `verification_timing` summary after joined cleanup: fixed stage names, cumulative seconds and
call counts only. No command arguments, environment values, paths, credentials or exception messages enter
this diagnostic. Context propagates to joined native threads and remains separate across concurrent checks.
Nested durations overlap; do not add them to calculate wall time. `child_observation` is the polling interval
until observed exit OR timeout/output cap, not an exact process lifetime. `cleanup` covers the final joined
cleanup block; earlier wait/job drain operations remain included in `native_operation`.

For first-call slowness distinguish native queue, runtime lock wait, staging, file pinning, complete hash/ACL
validation, AppContainer profile, spawn/resume and cleanup. Retain all failed attempts. No runtime integrity
checks or absolute deadlines may be removed to improve results. Diagnostic allocation/logging failure must
not discard a created resource, leak a lock or replace the original native/security error or cancellation.


## Owned Codex recovery test scope (WIP)

See OWNED_CODEX_RECOVERY.md. The 287-case selected recovery run combines contracts, auth/runtime pure
tests, Git security and three native pre-start filesystem refusal cases. It is not the entire backend or
all owned-runtime native cases. The separate active-link diagnostic failed its host positive control
before reaching a worker operation and remains unresolved; preserve it rather than calling it PASS.
Native owned-data cases require ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT pointing to a new private
Orchestrator-owned fixture outside source/task/TEMP roots; missing fixture is an explicit skip, never
a security PASS. Only run validation permitted by platform/project policy; do not evade a restriction.


## Codex activation refusal during recovery

Default Codex execution currently stops before inventory/spawn; installed/authenticated metadata is not
execution availability. A real Codex smoke cannot be counted PASS while this guard is active. Fake
transport parser contracts override only their test instance, with separate default-guard/task BLOCKED
tests. Do not launch real provider generation until the required runtime isolation gates pass.

## Owned production-flow integration coverage

`contract/test_codex_owned_execution.py` exercises the real adapter control flow with
synthetic authentication and fake contexts/processes: exec/resume named permission
selection, environment separation, empty inventory refusal, preparation joining,
pending and completed-spawn cancellation, early generator close, cleanup failure
precedence and withholding success until cleanup. These are not real model runs.

`security/test_cancelled_containment_result.py` verifies that timeout/heartbeat/user
cancellation cannot lose a containment error raised by the cancelled provider read,
and that an unfinished read never proves worktree ownership safe to release.

The explicitly selected owned native adapter cases use a real native context and
configuration/auth leases with synthetic auth, an installed CLI inventory helper,
and a fake provider stream. The native cancellation case instead uses a controlled
Python child, paused at native creation, to verify abort before it executes.
Neither category proves a Codex coding task reaches COMPLETED. The unresolved
active-link diagnostic is not included in that selected package; report deselection.


## Sandboxed process exit status

`security/test_sandbox_exit_status.py` separates fake Win32 status/query/error handling
from one explicitly Windows-native trusted ExitProcess(259) child. The latter tests
handle/status collection only, not untrusted-code containment or real provider execution.
A signaled handle establishes completion even when its exit status equals STILL_ACTIVE.
The runtime-lease final-review assertion retains the original write mask and deadline
and records additional handle/callback state if its unexplained failure recurs.
