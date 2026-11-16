import { describe, expect, it } from 'vitest';
import { TASK_STATES } from '../api/types';
import { ALLOWED_TRANSITIONS, allowedActions, canTransition, isActionAllowed } from './transitions';

describe('transition table', () => {
  it('covers all 13 states and terminal states have no exits', () => {
    expect(TASK_STATES).toHaveLength(13);
    expect(Object.keys(ALLOWED_TRANSITIONS).sort()).toEqual([...TASK_STATES].sort());
    expect(ALLOWED_TRANSITIONS.COMPLETED).toHaveLength(0);
    expect(ALLOWED_TRANSITIONS.CANCELLED).toHaveLength(0);
  });

  it('matches selected rows of states.py', () => {
    expect(canTransition('CREATED', 'QUEUED')).toBe(true);
    expect(canTransition('CREATED', 'RUNNING')).toBe(false);
    expect(canTransition('FAILED', 'QUEUED')).toBe(true);
    expect(canTransition('FAILED', 'RUNNING')).toBe(false);
    expect(canTransition('TESTING', 'COMPLETED')).toBe(true);
    expect(canTransition('PAUSED', 'RUNNING')).toBe(false);
  });
});

describe('action permissions', () => {
  it('start only from CREATED', () => {
    expect(TASK_STATES.filter((s) => isActionAllowed(s, 'start'))).toEqual(['CREATED']);
  });
  it('resume only from PAUSED', () => {
    expect(TASK_STATES.filter((s) => isActionAllowed(s, 'resume'))).toEqual(['PAUSED']);
  });
  it('retry from FAILED and BLOCKED', () => {
    expect(TASK_STATES.filter((s) => isActionAllowed(s, 'retry'))).toEqual(['BLOCKED', 'FAILED']);
  });
  it('pause where PAUSED is reachable', () => {
    expect(isActionAllowed('RUNNING', 'pause')).toBe(true);
    expect(isActionAllowed('QUEUED', 'pause')).toBe(true);
    expect(isActionAllowed('NEEDS_APPROVAL', 'pause')).toBe(false);
    expect(isActionAllowed('PAUSED', 'pause')).toBe(false);
    expect(isActionAllowed('COMPLETED', 'pause')).toBe(false);
  });
  it('cancel from every non-terminal state', () => {
    for (const s of TASK_STATES) expect(isActionAllowed(s, 'cancel')).toBe(s !== 'COMPLETED' && s !== 'CANCELLED');
  });
  it('terminal states allow nothing', () => {
    expect(allowedActions('COMPLETED')).toEqual([]);
    expect(allowedActions('CANCELLED')).toEqual([]);
  });
  it('every allowed action is a legal transition', () => {
    for (const s of TASK_STATES) if (allowedActions(s).length) expect(ALLOWED_TRANSITIONS[s].length).toBeGreaterThan(0);
  });
});
