/**
 * ALL API typing lives in this file. Field names mirror
 * backend/orchestrator/domain/models.py, events.py and api/routes/*.py (the
 * source of truth). Types for endpoints the backend has not implemented yet
 * (leadership, knowledge, analytics) are
 * marked "ASSUMED"; those calls stay behind "not available yet" handling.
 */

export const TASK_STATES = [
  'CREATED',
  'QUEUED',
  'PLANNING',
  'RUNNING',
  'TESTING',
  'REVIEWING',
  'WAITING_FOR_QUOTA',
  'NEEDS_APPROVAL',
  'PAUSED',
  'BLOCKED',
  'FAILED',
  'COMPLETED',
  'CANCELLED',
] as const;
export type TaskState = (typeof TASK_STATES)[number];

export const AGENT_ACTIVITIES = [
  'offline',
  'idle',
  'planning',
  'coding',
  'researching',
  'testing',
  'reviewing',
  'consulting',
  'rate_limited',
  'needs_approval',
  'blocked',
  'celebrating',
] as const;
export type AgentActivity = (typeof AGENT_ACTIVITIES)[number];

export type RunStatus = 'starting' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
export type RunPhase = 'plan' | 'execute' | 'repair' | 'review' | 'advise';
export type VerificationStatus = 'passed' | 'failed' | 'error' | 'no_checks' | 'unverifiable_accepted';

export type Exactness = 'exact' | 'measured' | 'estimated' | 'ai_evaluated' | 'unknown' | 'missing';

/** `{value, exactness, source}` shape from ARCHITECTURE section 6. */
export interface Measured {
  value: number | null;
  exactness: Exactness;
  source: string;
}

export interface VerifyCheckSpec {
  name?: string | null;
  cmd?: string[] | string | null;
  timeout?: number | null;
  file_exists?: string | null;
  contains?: string | null;
  file_absent?: string | null;
  file_unchanged?: string | null;
  json_valid?: string | null;
}
/** Who designed the checks of a verification: you, the leader (safety-filtered) or the leader and approved by you. */
export type VerifyOrigin = 'user' | 'leader' | 'approved';
export type VerifyCommand = string[] | string | VerifyCheckSpec;

export interface Project {
  id: string;
  name: string;
  path: string;
  default_branch: string | null;
  verify_commands: VerifyCommand[];
  skill_profile: Record<string, unknown>;
  is_git: boolean;
  isolation: 'worktree' | 'in_place';
  created_at: string;
  updated_at: string;
}

export interface Task {
  id: string;
  project_id: string;
  objective_id: string | null;
  title: string;
  description: string;
  task_type: string;
  priority: number;
  state: TaskState;
  state_reason: string | null;
  acceptance_criteria: string[];
  verify_commands: VerifyCommand[];
  plan: unknown;
  completed_steps: unknown[];
  pending_steps: unknown[];
  next_action: string | null;
  relevant_files: string[];
  role: string | null;
  preferred_provider: string | null;
  preferred_model: string | null;
  attempt_count: number;
  consecutive_failures: number;
  retry_count: number;
  repair_cycles: number;
  not_before: string | null;
  worktree_path: string | null;
  branch: string | null;
  base_commit: string | null;
  head_commit: string | null;
  active_seconds: number;
  last_activity_at: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  depends_on: string[];
  /** Client-derived from the event stream (never sent by the API); preserved across refetches. */
  current_operation?: string | null;
  provider?: string | null;
  model?: string | null;
  agent_id?: string | null;
  verification_status?: VerificationStatus | null;
  usage?: Record<string, unknown> | null;
}

export interface Run {
  id: string;
  task_id: string;
  phase: RunPhase;
  agent_id: string | null;
  provider: string;
  model: string | null;
  session_id: string | null;
  pid: number | null;
  status: RunStatus;
  error_category: string | null;
  error_message: string | null;
  exit_code: number | null;
  usage: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
  heartbeat_at: string | null;
  idempotency_key: string;
}

export interface StateTransition {
  id: number;
  task_id: string;
  from_state: string | null;
  to_state: string;
  reason: string | null;
  actor: string;
  ts: string;
}

export interface Checkpoint {
  id: string;
  task_id: string;
  run_id: string | null;
  kind: 'auto' | 'handoff' | 'leadership' | 'manual';
  summary: Record<string, unknown>;
  git_ref: string | null;
  created_at: string;
}

export interface CheckResult {
  name: string;
  kind: 'command' | 'file';
  argv: string[] | null;
  path: string | null;
  passed: boolean;
  exit_code: number | null;
  timed_out: boolean;
  duration_seconds: number;
  output: string;
  error: string | null;
}

export interface Verification {
  id: string;
  task_id: string;
  run_id: string | null;
  status: VerificationStatus;
  checks: CheckResult[];
  /** null on records from before the field existed */
  origin?: VerifyOrigin | null;
  started_at: string;
  finished_at: string | null;
}

/** GET /tasks/{id}/diff (api/routes/tasks.py). When `available` is false only `reason` and `changed_files` are set. */
export interface TaskDiff {
  available: boolean;
  reason?: string;
  base_commit?: string | null;
  head_commit?: string | null;
  branch?: string | null;
  stat?: string;
  diff?: string;
  changed_files: string[];
}

/** GET /tasks/{id}/artifacts (no `path`): changed files of the task workspace. */
export interface ArtifactList {
  root: string;
  files: { path: string; exists: boolean; size: number | null }[];
}
/** GET /tasks/{id}/artifacts?path= */
export interface ArtifactContent {
  path: string;
  size: number;
  content: string;
}

/** GET/PUT /settings: policy defaults (config/policies.yaml), runtime overrides and the merged effective values. */
export interface SettingsPayload {
  defaults: Record<string, Record<string, unknown>>;
  overrides: Record<string, Record<string, unknown>>;
  effective: Record<string, Record<string, unknown>>;
}

export interface Agent {
  id: string;
  name: string;
  role: string;
  room: string;
  provider: string | null;
  model: string | null;
  activity: AgentActivity;
  current_task_id: string | null;
  current_run_id: string | null;
  current_operation: string | null;
  since: string;
  seat_order: number;
}

export interface AgentInspectorData {
  id: string;
  name: string;
  role: string;
  room: string;
  activity: AgentActivity;
  status: string;
  provider: string | null;
  model: string | null;
  project_id: string | null;
  project_name: string | null;
  task_id: string | null;
  task_title: string | null;
  task_state: string | null;
  run_id: string | null;
  operation: string | null;
  since: string;
  elapsed_seconds: Measured;
  recent_events: ApiEvent[];
  changed_files: string[];
  test_results: Record<string, unknown> | null;
  usage: Record<string, unknown>;
}

export interface Approval {
  id: string;
  task_id: string | null;
  kind: string;
  summary: string;
  details: Record<string, unknown>;
  status: 'pending' | 'approved' | 'denied';
  requested_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  note: string | null;
}

export const USAGE_STATES = ['AVAILABLE', 'LOW', 'EXHAUSTED', 'COOLDOWN', 'UNKNOWN', 'AUTH_ERROR', 'PROVIDER_ERROR'] as const;
export type UsageState = (typeof USAGE_STATES)[number];

/** Persisted routing decision (GET /routing/decisions). `detail` is {classification, decision, reasons?, rule_ids?, fallback?}. */
export interface RoutingDecision {
  id: string;
  task_id: string;
  run_id: string | null;
  phase: string;
  chosen_provider: string;
  chosen_model: string | null;
  candidates: RankedCandidate[];
  reasons: string[];
  rule_ids: string[];
  adaptive_applied: boolean;
  detail: RoutingDetail;
  created_at: string;
}

export interface RankedCandidate {
  rank: number;
  provider: string;
  model: string;
  tier: string;
  score: number;
  components: Record<string, number>;
  adjustment?: number;
  adjustment_explanation?: string;
  usage_state?: string;
}
export interface RejectedCandidate {
  provider: string;
  model: string;
  filter: string;
  filters: string[];
  detail: string;
}
export interface TaskClassification {
  task_type: string;
  complexity: string;
  risk: string;
  required_capabilities: string[];
  signals: string[];
  role?: string | null;
}
/** Router output (orchestrator/router/router.py RoutingDecision). Persisted rows omit candidates/reasons/rule_ids/adaptive_applied here. */
export interface RouterDecision {
  provider: string | null;
  model: string | null;
  tier?: string | null;
  role?: string;
  phase?: string;
  task_type?: string;
  required_capabilities?: string[];
  min_tier?: string;
  target_tier?: string;
  candidates?: RankedCandidate[];
  rejected?: RejectedCandidate[];
  reasons?: string[];
  rule_ids?: string[];
  adaptive_applied?: boolean;
  requires_approval?: boolean;
  extra_verification?: boolean;
  no_candidate_reason?: string | null;
  wait_for_quota?: boolean;
  resets_at?: string | null;
  would_route_to?: string;
}
export interface RoutingDetail {
  classification?: TaskClassification;
  decision?: RouterDecision;
  reasons?: string[];
  rule_ids?: string[];
  fallback?: { action: string; category?: string; reason?: string };
  [k: string]: unknown;
}
export type RoutingOutcome = 'route' | 'wait_for_quota' | 'needs_approval' | 'blocked';
export interface RoutingPreview {
  outcome: RoutingOutcome;
  classification: TaskClassification;
  decision: RouterDecision;
}
export interface PreviewRequest {
  title: string;
  description?: string;
  task_type?: string | null;
  role?: string | null;
  phase: 'plan' | 'execute' | 'repair' | 'review' | 'advise';
}
export interface RoutingPreferences {
  provider_order: string[];
  disabled_providers: string[];
  disabled_models: string[];
  defaults: { provider_order: string[]; disabled_providers: string[]; disabled_models: string[] };
  overridden: string[];
}
export type PreferenceField = 'provider_order' | 'disabled_providers' | 'disabled_models';
/** PUT body: only the sent fields change; explicit null removes the override. */
export type PreferencesUpdate = Partial<Record<PreferenceField, string[] | null>>;

export interface ProviderCapabilities {
  structured_stream: boolean;
  resume: boolean;
  cancel: boolean;
  model_selection: boolean;
  model_discovery: boolean;
  usage_tokens: boolean;
  usage_cost: boolean;
  rate_limit_info: boolean;
  json_schema_output: boolean;
  sandbox_modes: string[];
  task_capabilities: string[];
}
export interface ProviderUsageState {
  state: UsageState;
  reason: string | null;
  resets_at: string | null;
  until: string | null;
  confidence: string | null;
  evidence_metric: string | null;
}
/** GET /providers (api/routes/providers.py). */
export interface ProviderStatus {
  provider: string;
  detected: boolean;
  installed: boolean;
  version: string | null;
  executable: string | null;
  authenticated: boolean | null;
  auth_detail: string | null;
  error: string | null;
  capabilities: ProviderCapabilities | null;
  billing_class: string | null;
  checked_at: string | null;
  model_count: number;
  usage: ProviderUsageState | null;
}

/** GET /models: one row per capability-registry entry. */
export interface ProviderModel {
  provider: string;
  model_id: string;
  alias: string | null;
  display_name: string | null;
  tier: string | null;
  tier_source: string | null;
  capabilities: string[];
  listed_capabilities: string[];
  billing_class: string | null;
  verification: string | null;
  source: string | null;
  discovered: boolean;
  provider_installed: boolean;
}

export interface UsageValue {
  metric: string;
  value: number | null;
  unit: string | null;
  exactness: 'exact' | 'estimated' | 'unknown';
  source: string;
  confidence: string | null;
  state: UsageState;
  resets_at: string | null;
  observed_at: string;
}
/** GET /usage entry: one per provider and per (provider, model) with model-scoped observations. */
export interface UsageEntry {
  provider: string;
  model: string | null;
  usage_state: ProviderUsageState;
  values: UsageValue[];
}

export const SKILL_STATES = [
  'CATALOGUED_ON_DEMAND',
  'DOWNLOADED_AND_DISABLED',
  'INSTALLED_AND_ACTIVE',
  'INSTALL_FAILED',
  'INCOMPATIBLE',
  'NEEDS_SECURITY_REVIEW',
] as const;
export type SkillState = (typeof SKILL_STATES)[number];
export const SKILL_KINDS = ['agent_skill', 'mcp_server', 'standalone_app', 'dev_tool', 'collection', 'external_service', 'docs'] as const;

export interface SkillWarning {
  type: 'duplicate' | 'conflict';
  reason: string;
  with: string[];
  name?: string;
  source?: string;
}
export interface SkillActivationInfo {
  possible: boolean;
  mode: string;
  reason?: string;
  folder?: string;
  file?: string;
  server?: string;
}
/** GET /skills row (skills/views.py summary). */
export interface Skill {
  id: string;
  name: string;
  user_label: string | null;
  category: string | null;
  kind: string;
  description: string;
  state: SkillState;
  source_url: string | null;
  source_verified: boolean;
  source: string;
  installable: boolean;
  installable_reason: string | null;
  activation: SkillActivationInfo;
  pinned_ref: string | null;
  installed_ref: string | null;
  previous_ref: string | null;
  compat: Record<string, unknown>;
  risk: string | null;
  requires_credentials: boolean;
  has_uncertainty: boolean;
  last_error: string | null;
  incompatible_reasons: string[];
  active_projects: string[];
  warnings: SkillWarning[];
  updated_at: string;
  /** only with ?project_id= */
  project_activation?: { active: boolean; ref?: string; name?: string; mode?: string; activated_at?: string };
}
export interface SkillFinding {
  severity: string;
  code: string;
  path: string;
  message: string;
}
export interface SkillSecurity {
  inspected?: boolean;
  risk?: string;
  findings?: SkillFinding[];
  review_reasons?: string[];
  requires_credentials?: boolean;
  needs_review?: boolean;
  scripts?: string[];
  native_binaries?: string[];
  hooks?: string[];
  package_scripts?: { path: string; name: string; command: string }[];
  mcp_servers?: { path: string; name: string; command: string; args: string[] }[];
  catalog_notes?: string;
  [k: string]: unknown;
}
export interface SkillActivationRecord {
  project_id: string;
  scope: string;
  ref: string;
  name: string;
  mode: string;
  targets?: string[];
  approval_id?: string;
  activated_at: string;
  artifacts: { type: string; target?: string; path: string; file_count?: number; server?: string; created_file?: boolean }[];
}
export interface SkillEvent {
  id: string;
  skill_id: string;
  action: string;
  from_state: string | null;
  to_state: string | null;
  ref: string | null;
  detail: unknown;
  ts: string;
}
/** GET /skills/{id} (views.detail). */
export interface SkillDetail extends Skill {
  uncertainty: string | null;
  candidates: string[];
  license: string | null;
  maintenance: Record<string, unknown>;
  default_branch: string | null;
  paid_service: string | null;
  priority_candidate: boolean;
  duplicates_of: string | null;
  install_method: { type?: string; command?: string | null; notes?: string | null; [k: string]: unknown };
  dependencies: unknown[];
  permissions: unknown[];
  security: SkillSecurity;
  local_path: string | null;
  archives: { ref: string; files?: number; bytes?: number; fetched_at?: string }[];
  pending_reactivation: unknown[];
  activations: SkillActivationRecord[];
  events: SkillEvent[];
  instructions: {
    documented_method: { type?: string | null; command?: string | null; notes?: string | null };
    activation: SkillActivationInfo;
    orchestrator_steps: string[];
    note: string;
  };
  codex: { supported: boolean; reason?: string; location?: string; note?: string };
}
export interface SkillOutcome {
  action?: string;
  ok?: boolean;
  changed?: boolean;
  status?: string;
  approval_id?: string;
  message?: string;
  reason?: string;
  note?: string;
  state?: string;
  ref?: string;
  [k: string]: unknown;
}
export interface SkillActionResult {
  outcome: SkillOutcome;
  skill: SkillDetail;
}
export interface SkillContent {
  skill_id: string;
  ref: string | null;
  path: string;
  text: string;
  truncated: boolean;
}
export type SkillActionName = 'install' | 'activate' | 'deactivate' | 'update' | 'rollback';

/** GET /status (api/routes/system.py). `last_seq` is the resume point for the WebSocket replay. */
export interface SystemStatus {
  version: string;
  uptime_seconds: Measured;
  scheduler: { running: boolean; assignment_paused: boolean; active_tasks: string[] };
  providers: string[];
  recovery: unknown;
  task_counts: Record<string, number>;
  pending_approvals: number;
  live_runs: number;
  last_seq: number;
  projects: number;
  leader: { provider: string; model: string | null } | null;
  /** `detecting` while the default leader is still being chosen; then `set` or `none`. */
  leader_state?: LeaderState;
}

export type LeaderState = "detecting" | "none" | "set";

export interface ApiEvent {
  seq: number;
  ts: string;
  type: string;
  project_id?: string | null;
  task_id?: string | null;
  run_id?: string | null;
  agent_id?: string | null;
  payload: Record<string, unknown>;
}

/** WebSocket control frames (api/ws.py). They carry no entity data and no event seq of their own. */
export type WsControl =
  | { type: 'replay.done'; seq: number; truncated?: boolean }
  | { type: 'ping'; seq: number }
  | { type: 'replay.truncated'; seq: number; requested_after_seq: number; resume_after_seq: number; skipped: number; cap: number };

/** Event payload shapes (domain/events.py + emitters). Fields not listed are not consumed. */
export interface TaskStateChangedPayload {
  from: TaskState | null;
  to: TaskState;
  reason: string | null;
  actor: string;
}
export interface RunStartedPayload {
  phase: RunPhase;
  provider: string;
  model: string | null;
  idempotency_key: string;
}
export interface RunOutputPayload {
  text: string;
}
export interface RunToolUsePayload {
  tool: string;
  summary: string;
  files?: string[];
}
export interface ApprovalResolvedPayload {
  approval_id: string;
  kind: string;
  approved: boolean;
  by: string;
  note?: string | null;
}

export interface ErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

// ---- request DTOs ----

export interface ProjectCreate {
  name: string;
  path: string;
  default_branch?: string | null;
  verify_commands?: VerifyCommand[];
  skill_profile?: Record<string, unknown>;
}
export interface ProjectUpdate {
  name?: string;
  default_branch?: string | null;
  verify_commands?: VerifyCommand[];
  skill_profile?: Record<string, unknown>;
}
export interface TaskCreate {
  project_id: string;
  title: string;
  description?: string;
  task_type?: string;
  priority?: number;
  objective_id?: string | null;
  acceptance_criteria?: string[];
  verify_commands?: VerifyCommand[];
  relevant_files?: string[];
  role?: string | null;
  preferred_provider?: string | null;
  preferred_model?: string | null;
  depends_on?: string[];
  autostart?: boolean;
}
export type TaskUpdate = Partial<Omit<TaskCreate, 'project_id' | 'autostart' | 'objective_id'>> & {
  next_action?: string | null;
};
export type TaskAction = 'start' | 'pause' | 'resume' | 'cancel' | 'retry';

// ---------------------------------------------------------------------------------------------------------------
// M4d areas: Leadership, Objectives, Knowledge, Analytics. Shapes come from backend/orchestrator/api/routes/
// {leadership,objectives,knowledge,analytics}.py and leader/*.py, knowledge/service.py, analytics/{metrics,adaptive}.py.
// Everything for these areas lives here so a backend change is a one-file adjustment.
// ---------------------------------------------------------------------------------------------------------------

// ---- Leadership (GET/PUT /leadership, POST /leadership/switch, POST /advisor/consult, /decisions) ----
export type AdvisorPolicy = 'never' | 'on_request' | 'auto';
export type FallbackPolicy = 'off' | 'same_provider_lower_tier' | 'any_capable';
export const ADVISOR_POLICIES: AdvisorPolicy[] = ['never', 'on_request', 'auto'];
export const FALLBACK_POLICIES: FallbackPolicy[] = ['off', 'same_provider_lower_tier', 'any_capable'];

export interface LeaderIdentity {
  provider: string;
  model: string | null;
}
export interface LeaderInfo extends LeaderIdentity {
  tier?: string | null;
  /** 'default' | 'manual' | 'rollback' | 'fallback' ... (leadership.leader_meta) */
  source?: string;
  reason?: string;
  set_at?: string;
}
export interface LeaderCandidate {
  provider: string;
  model: string;
  tier: string | null;
  eligible: boolean;
  reasons: string[];
  filters?: string[];
  score?: number;
}
export interface LeadershipBudgets {
  consultations_today: { used: number; limit: number };
  consultations_per_task: { limit: number };
  leader_calls_per_task: { limit: number };
  max_parallel_workers: { limit: number };
}
export interface LeadershipSettings {
  advisor_policy: AdvisorPolicy;
  fallback_policy: FallbackPolicy;
  max_consultations_per_day: number;
  advisor_after_failed_repairs: number;
  skip_planning_for_small_tasks: boolean;
  max_leader_calls_per_task: number;
  max_consultations_per_task: number;
  max_parallel_workers: number;
}
export interface Leadership {
  leader: LeaderInfo | null;
  advisor: LeaderIdentity | null;
  advisor_effective: 'configured' | 'router' | 'none';
  settings: LeadershipSettings;
  role_minimum_tier: { leader: string | null; advisor: string | null };
  budgets: LeadershipBudgets;
  permissions: { approvals_required_for: string[]; auto_approve: string[]; worker_permissions: Record<string, unknown> };
  candidates: { leader: LeaderCandidate[]; advisor: LeaderCandidate[] };
  assignment_paused: boolean;
  switch_in_progress: boolean;
}
/** PUT /leadership body: only sent fields change; `advisor: null` removes the advisor. */
export type LeadershipUpdate = Partial<LeadershipSettings> & { advisor?: { provider: string; model: string } | null };

export interface SwitchResult {
  status: 'switched' | 'rolled_back';
  from: LeaderIdentity | null;
  to: LeaderIdentity;
  checkpoint_id: string | null;
  validated: boolean;
  skip_validation: boolean;
  attempts: number;
  /** switched: the reason given; rolled_back: why validation failed */
  reason: string;
  leader?: LeaderIdentity;
}

export interface AdvisorResult {
  assessment: string;
  recommended_option: string;
  risks: string[];
  disagreements: string[];
  confidence: number;
  evidence_requested: string[];
}
export interface ConsultRequest {
  task_id?: string | null;
  problem: string;
  options: string[];
  constraints: string[];
}
export interface ConsultResponse {
  decision_id: string;
  run_id: string;
  trigger: string;
  provider: string;
  model: string | null;
  independence: 'different_provider' | 'same_provider';
  evidence_status: string;
  result: AdvisorResult;
  budget: LeadershipBudgets;
}

export interface EvidenceRef {
  type: string;
  id?: string;
  [k: string]: unknown;
}
export interface Decision {
  id: string;
  project_id: string | null;
  task_id: string | null;
  title: string;
  context: string;
  options: unknown[];
  decision: string;
  rationale: string;
  evidence: EvidenceRef[];
  advisor_run_id: string | null;
  kind: 'advisor_consult' | 'leader_decision' | string;
  /** "verified" = cites a verification/commit/diff; "none" = flagged, no objective evidence */
  evidence_status: 'verified' | 'none' | string;
  meta: Record<string, unknown>;
  created_at: string;
}

// ---- Objectives ----
export type ObjectiveStatus = 'planning' | 'planned' | 'running' | 'completed' | 'failed';
export interface ObjectiveTask {
  id: string;
  key: string | null;
  title: string;
  state: TaskState;
  task_type: string;
  role: string | null;
  priority: number;
  depends_on: string[];
  verify_commands?: VerifyCommand[];
}
export interface Objective {
  id: string;
  project_id: string;
  text: string;
  status: ObjectiveStatus | string;
  deliverables: string[];
  acceptance_criteria: string[];
  prerequisites: string[];
  error: string | null;
  /** task key -> verify commands the leader proposed but the safety check did not adopt */
  unsafe_verify_commands: Record<string, { command: string; reason: string }[]>;
  run_ids: string[];
  tasks: ObjectiveTask[];
  task_counts: Record<string, number>;
  created_at: string;
  updated_at: string;
}

// ---- Knowledge ----
export type KnowledgeKind = 'requirement' | 'adr' | 'doc' | 'task' | 'error_resolution' | 'code_meta' | 'skill' | 'model_history';
export const KNOWLEDGE_KINDS: KnowledgeKind[] = ['requirement', 'adr', 'doc', 'task', 'error_resolution', 'code_meta', 'skill', 'model_history'];
export type KnowledgeSource = 'docs' | 'tasks' | 'errors' | 'code' | 'skills' | 'decisions' | 'models';
export const KNOWLEDGE_SOURCES: KnowledgeSource[] = ['docs', 'tasks', 'errors', 'code', 'skills', 'decisions', 'models'];

export interface KnowledgeHit {
  doc_id: string;
  kind: string;
  title: string;
  source_path: string | null;
  project_id: string | null;
  updated_at: string;
  score: number;
  /** Plain text; matches are wrapped in « » (U+00AB / U+00BB). Never HTML. */
  snippet: string;
}
export interface KnowledgeSearchResponse {
  results: KnowledgeHit[];
  used: { fts: boolean; vectors: boolean };
}
export interface KnowledgeDocSummary {
  id: string;
  project_id: string | null;
  kind: string;
  title: string;
  source_path: string | null;
  source_hash: string;
  updated_at: string;
  chars: number;
}
export interface KnowledgeDoc extends Omit<KnowledgeDocSummary, 'chars'> {
  body: string;
}
export interface KnowledgeDocsPage {
  total: number;
  limit: number;
  offset: number;
  docs: KnowledgeDocSummary[];
}
export interface IndexReport {
  added: number;
  updated: number;
  removed: number;
  skipped: number;
  errors: string[];
  skipped_details: { path: string; reason: string }[];
}
export interface IndexJob {
  job_id: string;
  status: 'running' | 'done' | 'failed' | string;
  report: IndexReport | null;
}
export interface KnowledgeStats {
  docs: number;
  by_kind: Record<string, number>;
  by_project: Record<string, number>;
  chars: number;
  last_updated: string | null;
  vectors: number;
  vector_search: { enabled: boolean; kinds: string[]; embedder: string; semantic: boolean };
}

// ---- Analytics ----
export type MetricExactness = 'measured' | 'estimated' | 'ai_evaluated' | 'missing';
export interface Metric<V = number | null> {
  value: V;
  exactness: MetricExactness | string;
  source: string;
  n: number;
}
export interface Percentiles {
  p50: number | null;
  p90: number | null;
  mean: number | null;
}
/** analytics/metrics.py provider_availability[provider]. `available_share` is null when every observation was UNKNOWN. */
export interface ProviderAvailability {
  available_share: number | null;
  known_observations: number;
  unknown_observations: number;
  /** known + unknown */
  observations: number;
  last_observed_at: string | null;
  exactness: string;
}
export interface SizeDistribution {
  p50: number | null;
  p90: number | null;
  mean: number | null;
  max: number | null;
}
export interface HandoffPromptChars {
  prompt_chars: SizeDistribution | null;
  handoff_chars: SizeDistribution | null;
  knowledge_chars: SizeDistribution | null;
}
export interface TokensValue {
  input: number;
  output: number;
  cached: number;
  by_model: Record<string, { input: number; output: number; cached: number | null; runs: number }>;
}
export interface AnalyticsSummary {
  scope: { project_id: string | null; provider: string | null; model: string | null; task_type: string | null; from: string | null; to: string | null };
  metrics: {
    tasks_completed: Metric;
    verified_completion_rate: Metric;
    test_pass_rate: Metric;
    retries_total: Metric;
    retries_per_task: Metric;
    repair_cycles_total: Metric;
    failure_frequency: Metric<Record<string, number> | null>;
    run_duration_seconds: Metric<Percentiles | null>;
    task_active_seconds: Metric<Percentiles | null>;
    provider_model_switches: Metric<{ total: number; per_task_avg: number; tasks_with_switch: number } | null>;
    tokens: Metric<TokensValue | null>;
    cost_usd_estimated: Metric;
    provider_availability: Metric<Record<string, ProviderAvailability> | null>;
    handoff_checkpoints: Metric;
    handoff_prompt_chars: Metric<HandoffPromptChars | null>;
  };
}
export interface AnalyticsFilters {
  project_id?: string;
  provider?: string;
  model?: string;
  task_type?: string;
  from?: string;
  to?: string;
}
export const TIMESERIES_METRICS = ['tasks_completed', 'runs_started', 'runs_failed', 'test_pass_rate', 'tokens_input', 'tokens_output', 'cost_usd_estimated'] as const;
export type TimeseriesMetric = (typeof TIMESERIES_METRICS)[number];
export interface TimeseriesPoint {
  bucket: string;
  value: number;
  n: number;
}
export interface Timeseries {
  metric: string;
  points: TimeseriesPoint[];
  exactness: string;
  source: string;
  n: number;
}
export interface ModelRow {
  provider: string;
  model: string;
  task_type: string;
  runs: number;
  verified_runs: number;
  successes: number;
  success_rate: Metric;
  avg_duration_seconds: Metric;
  p50_duration_seconds: Metric;
  avg_tokens: Metric;
  last_used: string | null;
}
export interface ModelsResponse {
  models: ModelRow[];
  definition: string;
}
export interface AdaptiveSettings {
  enabled: boolean;
  min_samples: number;
  max_adjustment: number;
  overrides: Record<string, unknown>;
  bounds: { min_samples: [number, number]; max_adjustment: [number, number] };
  effective_cap_points: number;
  note: string;
}
export interface AdaptiveCell {
  provider: string;
  model: string;
  task_type: string;
  runs: number;
  verified_runs: number;
  successes: number;
  min_samples: number;
  threshold_met: boolean;
  success_rate: Metric;
  adjustment: { points: number; applied: boolean; explanation: string };
}
export interface AdaptiveHistoryRow {
  id: string;
  task_id: string;
  run_id: string | null;
  phase: string;
  provider: string;
  model: string | null;
  created_at: string;
  adaptive_reasons: string[];
}
export interface AnalyticsRouting {
  settings: AdaptiveSettings;
  cells: AdaptiveCell[];
  applied_history: AdaptiveHistoryRow[];
  decisions_with_adaptive: number;
}
export interface AdaptiveSettingsUpdate {
  enabled?: boolean;
  min_samples?: number;
  max_adjustment?: number;
}
export interface AgentStatsRow {
  agent_id: string;
  name: string;
  role: string;
  room: string | null;
  provider: string | null;
  model: string | null;
  activity: string;
  runs: Metric;
  succeeded: Metric;
  failed: Metric;
  working_seconds: Metric;
  last_run_at: string | null;
}
