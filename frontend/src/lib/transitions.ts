/**
 * Mirror of backend/orchestrator/domain/states.py (ALLOWED_TRANSITIONS & co).
 * This is the ONLY place in the frontend that encodes the state machine.
 * Keep it in sync with states.py (the backend contract wins).
 */
import type { TaskAction, TaskState } from '../api/types';

export const ALLOWED_TRANSITIONS: Record<TaskState, readonly TaskState[]> = {
  CREATED: ['QUEUED', 'CANCELLED'],
  QUEUED: ['PLANNING', 'RUNNING', 'PAUSED', 'BLOCKED', 'NEEDS_APPROVAL', 'CANCELLED'],
  PLANNING: ['RUNNING', 'QUEUED', 'WAITING_FOR_QUOTA', 'NEEDS_APPROVAL', 'PAUSED', 'BLOCKED', 'FAILED', 'CANCELLED'],
  RUNNING: ['TESTING', 'QUEUED', 'WAITING_FOR_QUOTA', 'NEEDS_APPROVAL', 'PAUSED', 'BLOCKED', 'FAILED', 'CANCELLED'],
  TESTING: ['REVIEWING', 'RUNNING', 'COMPLETED', 'QUEUED', 'NEEDS_APPROVAL', 'PAUSED', 'BLOCKED', 'FAILED', 'CANCELLED'],
  REVIEWING: ['COMPLETED', 'RUNNING', 'QUEUED', 'WAITING_FOR_QUOTA', 'NEEDS_APPROVAL', 'PAUSED', 'BLOCKED', 'FAILED', 'CANCELLED'],
  WAITING_FOR_QUOTA: ['QUEUED', 'PAUSED', 'FAILED', 'CANCELLED'],
  NEEDS_APPROVAL: ['QUEUED', 'COMPLETED', 'BLOCKED', 'FAILED', 'CANCELLED'],
  PAUSED: ['QUEUED', 'CANCELLED'],
  BLOCKED: ['QUEUED', 'FAILED', 'CANCELLED'],
  FAILED: ['QUEUED', 'CANCELLED'],
  COMPLETED: [],
  CANCELLED: [],
};

export const TERMINAL_STATES: readonly TaskState[] = ['COMPLETED', 'CANCELLED'];
export const ACTIVE_STATES: readonly TaskState[] = ['PLANNING', 'RUNNING', 'TESTING', 'REVIEWING'];

export function canTransition(current: TaskState, target: TaskState): boolean {
  return ALLOWED_TRANSITIONS[current]?.includes(target) ?? false;
}

export function isActiveState(s: TaskState): boolean {
  return ACTIVE_STATES.includes(s);
}

export function isTerminalState(s: TaskState): boolean {
  return TERMINAL_STATES.includes(s);
}

/**
 * Each user action maps to a target state plus the source states it is meant
 * for (a pure "can transition" check would also allow e.g. start from PAUSED).
 */
const ACTION_RULES: Record<TaskAction, { target: TaskState; from: readonly TaskState[] | 'any' }> = {
  start: { target: 'QUEUED', from: ['CREATED'] },
  pause: { target: 'PAUSED', from: 'any' },
  resume: { target: 'QUEUED', from: ['PAUSED'] },
  cancel: { target: 'CANCELLED', from: 'any' },
  retry: { target: 'QUEUED', from: ['FAILED', 'BLOCKED'] },
};

export const TASK_ACTIONS: readonly TaskAction[] = ['start', 'pause', 'resume', 'cancel', 'retry'];

export function actionTarget(action: TaskAction): TaskState {
  return ACTION_RULES[action].target;
}

export function isActionAllowed(state: TaskState, action: TaskAction): boolean {
  const rule = ACTION_RULES[action];
  if (rule.from !== 'any' && !rule.from.includes(state)) return false;
  return canTransition(state, rule.target);
}

export function allowedActions(state: TaskState): TaskAction[] {
  return TASK_ACTIONS.filter((a) => isActionAllowed(state, a));
}
