/** Animation frame selection. Pure functions of (anim, elapsed seconds). */
import { ANIM_FRAMES, type AnimName } from './types';

const FPS: Record<AnimName, number> = {
  idle: 2,
  walk: 8,
  type: 7,
  read: 1.2,
  review: 2,
  test: 5,
  talk: 3,
  celebrate: 6,
  sleep: 0.8,
};

/** Idle timeline: breathing (frame 0/1) with a blink (frame 2) every few seconds. */
const IDLE_TIMELINE = [0, 0, 1, 1, 0, 0, 1, 1, 0, 2, 0, 1, 1, 0, 0, 1];

export function frameFor(anim: AnimName, t: number, reduced = false): number {
  if (reduced) return 0;
  const n = ANIM_FRAMES[anim];
  const step = Math.floor(Math.max(0, t) * FPS[anim]);
  if (anim === 'idle') return IDLE_TIMELINE[step % IDLE_TIMELINE.length];
  return step % n;
}

/** Vertical jump offset in px (negative = up) for the celebrate animation. */
export function jumpOffset(f: number): number {
  return [0, -3, -6, -3][f % 4] ?? 0;
}
