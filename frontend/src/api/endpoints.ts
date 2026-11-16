import { request, unwrapList } from './client';
import type {
  Agent,
  AgentInspectorData,
  ApiEvent,
  Approval,
  ArtifactContent,
  ArtifactList,
  AdaptiveSettings,
  AdaptiveSettingsUpdate,
  AgentStatsRow,
  AnalyticsFilters,
  AnalyticsRouting,
  AnalyticsSummary,
  Checkpoint,
  ConsultRequest,
  ConsultResponse,
  Decision,
  IndexJob,
  KnowledgeDoc,
  KnowledgeDocsPage,
  KnowledgeSearchResponse,
  KnowledgeStats,
  Leadership,
  LeadershipUpdate,
  ModelsResponse,
  Objective,
  SwitchResult,
  Timeseries,
  PreferencesUpdate,
  PreviewRequest,
  Project,
  ProjectCreate,
  ProjectUpdate,
  ProviderModel,
  ProviderStatus,
  RoutingDecision,
  RoutingPreferences,
  RoutingPreview,
  Run,
  Skill,
  SkillActionName,
  SkillActionResult,
  SkillContent,
  SkillDetail,
  StateTransition,
  SettingsPayload,
  SystemStatus,
  Task,
  TaskAction,
  TaskCreate,
  TaskDiff,
  TaskUpdate,
  UsageEntry,
  Verification,
} from './types';

const enc = encodeURIComponent;
const list = <T>(p: Promise<unknown>): Promise<T[]> => p.then((d) => unwrapList<T>(d));

/** `worker_permissions.claude_shell` is YAML-only: the API answers 422 to it, so it is never sent. */
export function stripYamlOnly(values: Record<string, unknown>): Record<string, unknown> {
  const wp = values.worker_permissions;
  if (!wp || typeof wp !== 'object' || !('claude_shell' in wp)) return values;
  const { claude_shell: _drop, ...rest } = wp as Record<string, unknown>;
  void _drop;
  return { ...values, worker_permissions: rest };
}

export const api = {
  // System
  health: () => request<{ status?: string }>('/health'),
  status: () => request<SystemStatus>('/status'),
  getSettings: () => request<SettingsPayload>('/settings'),
  putSettings: (values: Record<string, unknown>) => request<SettingsPayload>('/settings', { method: 'PUT', body: { values: stripYamlOnly(values) } }),

  // Providers
  providers: () => list<ProviderStatus>(request('/providers')),
  refreshProviders: () => list<ProviderStatus>(request('/providers/refresh', { method: 'POST' })),
  models: () => list<ProviderModel>(request('/models')),
  usage: () => list<UsageEntry>(request('/usage')),

  // Projects
  projects: () => list<Project>(request('/projects')),
  createProject: (b: ProjectCreate) => request<Project>('/projects', { method: 'POST', body: b }),
  project: (id: string) => request<Project>(`/projects/${enc(id)}`),
  updateProject: (id: string, b: ProjectUpdate) => request<Project>(`/projects/${enc(id)}`, { method: 'PATCH', body: b }),
  deleteProject: (id: string) => request<void>(`/projects/${enc(id)}`, { method: 'DELETE' }),

  // Objectives
  createObjective: (projectId: string, b: { text: string; autostart?: boolean; wait?: boolean }) =>
    request<Objective>(`/projects/${enc(projectId)}/objectives`, { method: 'POST', body: b }),
  objectives: (projectId: string) => list<Objective>(request(`/projects/${enc(projectId)}/objectives`)),
  objective: (id: string) => request<Objective>(`/objectives/${enc(id)}`),

  // Tasks
  tasks: (q: { project_id?: string; state?: string } = {}) => list<Task>(request('/tasks', { query: q })),
  createTask: (b: TaskCreate) => request<Task>('/tasks', { method: 'POST', body: b }),
  task: (id: string) => request<Task>(`/tasks/${enc(id)}`),
  updateTask: (id: string, b: TaskUpdate) => request<Task>(`/tasks/${enc(id)}`, { method: 'PATCH', body: b }),
  taskAction: (id: string, action: TaskAction) => request<Task>(`/tasks/${enc(id)}/${action}`, { method: 'POST' }),
  taskEvents: (id: string) => list<ApiEvent>(request(`/tasks/${enc(id)}/events`)),
  taskTransitions: (id: string) => list<StateTransition>(request(`/tasks/${enc(id)}/transitions`)),
  taskRuns: (id: string) => list<Run>(request(`/tasks/${enc(id)}/runs`)),
  taskCheckpoints: (id: string) => list<Checkpoint>(request(`/tasks/${enc(id)}/checkpoints`)),
  createCheckpoint: (id: string, label: string) => request<Checkpoint>(`/tasks/${enc(id)}/checkpoints`, { method: 'POST', body: { label } }),
  restoreCheckpoint: (id: string, checkpointId: string) =>
    request<Checkpoint>(`/tasks/${enc(id)}/checkpoints/${enc(checkpointId)}/restore`, { method: 'POST' }),
  taskDiff: (id: string) => request<TaskDiff>(`/tasks/${enc(id)}/diff`),
  taskArtifacts: (id: string) => request<ArtifactList>(`/tasks/${enc(id)}/artifacts`),
  taskArtifact: (id: string, path: string) => request<ArtifactContent>(`/tasks/${enc(id)}/artifacts`, { query: { path } }),
  taskVerification: (id: string) => list<Verification>(request(`/tasks/${enc(id)}/verification`)),

  // Agents
  agents: () => list<Agent>(request('/agents')),
  agent: (id: string) => request<AgentInspectorData>(`/agents/${enc(id)}`),

  // Events
  events: (q: { after_seq?: number; limit?: number; task_id?: string } = {}) => list<ApiEvent>(request('/events', { query: q })),

  // Approvals
  approvals: (status?: string) => list<Approval>(request('/approvals', { query: { status } })),
  resolveApproval: (id: string, approve: boolean, note?: string) =>
    request<Approval>(`/approvals/${enc(id)}/resolve`, { method: 'POST', body: { approve, note: note || null } }),

  // Leadership
  leadership: () => request<Leadership>('/leadership'),
  putLeadership: (b: LeadershipUpdate) => request<Leadership>('/leadership', { method: 'PUT', body: b }),
  switchLeader: (b: { provider: string; model: string }) => request<SwitchResult>('/leadership/switch', { method: 'POST', body: b }),
  consultAdvisor: (b: ConsultRequest) => request<ConsultResponse>('/advisor/consult', { method: 'POST', body: b }),
  decisions: (q: { project_id?: string; task_id?: string; limit?: number } = {}) => list<Decision>(request('/decisions', { query: { limit: 200, ...q } })),
  exportDecision: (id: string) => request<{ decision_id: string; path: string; appended: boolean }>(`/decisions/${enc(id)}/export`, { method: 'POST' }),

  // Routing
  routingDecisions: (q: { task_id?: string; limit?: number } = {}) => list<RoutingDecision>(request('/routing/decisions', { query: q })),
  routingPreview: (b: PreviewRequest) => request<RoutingPreview>('/routing/preview', { method: 'POST', body: b }),
  routingPreferences: () => request<RoutingPreferences>('/routing/preferences'),
  putRoutingPreferences: (b: PreferencesUpdate) => request<RoutingPreferences>('/routing/preferences', { method: 'PUT', body: b }),

  // Knowledge
  // (full-text search; optional non-semantic hash vectors)
  knowledgeSearch: (b: { query: string; project_id?: string | null; kinds?: string[]; limit?: number }) =>
    request<KnowledgeSearchResponse>('/knowledge/search', { method: 'POST', body: b }),
  knowledgeIndex: (b: { project_id?: string | null; sources?: string[]; full?: boolean }) =>
    request<{ job_id: string; status: string }>('/knowledge/index', { method: 'POST', body: b }),
  knowledgeJob: (id: string) => request<IndexJob>(`/knowledge/index/${enc(id)}`),
  knowledgeDocs: (q: { project_id?: string; kind?: string; limit?: number; offset?: number } = {}) =>
    request<KnowledgeDocsPage>('/knowledge/docs', { query: q }),
  knowledgeDoc: (id: string) => request<KnowledgeDoc>(`/knowledge/docs/${enc(id)}`),
  deleteKnowledgeDoc: (id: string) => request<void>(`/knowledge/docs/${enc(id)}`, { method: 'DELETE' }),
  knowledgeStats: () => request<KnowledgeStats>('/knowledge/stats'),

  // Analytics (every metric is {value, exactness, source, n})
  analyticsSummary: (q: AnalyticsFilters = {}) => request<AnalyticsSummary>('/analytics/summary', { query: { ...q } }),
  analyticsModels: () => request<ModelsResponse>('/analytics/models'),
  analyticsRouting: () => request<AnalyticsRouting>('/analytics/routing'),
  analyticsTimeseries: (metric: string, q: AnalyticsFilters = {}) =>
    request<Timeseries>('/analytics/timeseries', { query: { metric, bucket: 'day', ...q } }),
  analyticsAgents: (q: AnalyticsFilters = {}) => request<{ agents: AgentStatsRow[] }>('/analytics/agents', { query: { ...q } }),
  analyticsAdaptiveSettings: () => request<AdaptiveSettings>('/analytics/adaptive-settings'),
  putAnalyticsAdaptiveSettings: (b: AdaptiveSettingsUpdate) =>
    request<AdaptiveSettings>('/analytics/adaptive-settings', { method: 'PUT', body: b }),

  // Skills
  skills: (q: { q?: string; category?: string; kind?: string; state?: string; project_id?: string } = {}) => list<Skill>(request('/skills', { query: q })),
  skill: (id: string) => request<SkillDetail>(`/skills/${enc(id)}`),
  skillContent: (id: string) => request<SkillContent>(`/skills/${enc(id)}/content`),
  setSkillSource: (id: string, source_url: string) =>
    request<SkillActionResult>(`/skills/${enc(id)}`, { method: 'PATCH', body: { source_url } }),
  skillAction: (id: string, action: SkillActionName, body?: Record<string, unknown>) =>
    request<SkillActionResult>(`/skills/${enc(id)}/${action}`, { method: 'POST', body }),
};

export type Api = typeof api;
