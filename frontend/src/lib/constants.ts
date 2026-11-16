/** Mirrors config/routing.yaml `task_types` and `roles` (config seeds; not a backend contract). */
export const TASK_TYPES = [
  'planning',
  'architecture',
  'coding',
  'frontend',
  'testing',
  'review',
  'research',
  'documentation',
  'visual_asset',
  'browser_check',
] as const;

export const ROLES = [
  'leader',
  'advisor',
  'core_systems',
  'backend',
  'frontend',
  'pixel_office',
  'test',
  'security',
  'research',
  'visual',
] as const;
