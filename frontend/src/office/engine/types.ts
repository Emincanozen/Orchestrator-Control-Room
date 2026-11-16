/** Shared engine types. Pure data: no Pixi, no DOM. */
export type Dir = 'up' | 'down' | 'left' | 'right';
export type Pose = 'stand' | 'sit';
export type AnimName = 'idle' | 'walk' | 'type' | 'read' | 'review' | 'test' | 'talk' | 'celebrate' | 'sleep';
export type Indicator = 'warning' | 'approval' | 'blocked' | 'sleep' | 'speech';
export type ScreenMode = 'off' | 'code' | 'doc' | 'review';

/** Number of frames per animation (all frames exist in the generated atlas). */
export const ANIM_FRAMES: Record<AnimName, number> = {
  idle: 3,
  walk: 4,
  type: 2,
  read: 2,
  review: 2,
  test: 2,
  talk: 2,
  celebrate: 4,
  sleep: 2,
};

export const TILE = 16;

export interface Tile {
  x: number;
  y: number;
}

export interface FrameSpec {
  pose: Pose;
  dir: Dir;
  anim: AnimName;
  f: number;
}

export const frameKey = (s: FrameSpec) => `${s.pose}|${s.dir}|${s.anim}|${s.f}`;

/** The subset of an `Agent` row the engine reads. */
export interface OfficeAgent {
  id: string;
  name: string;
  role: string;
  room: string;
  provider: string | null;
  model: string | null;
  activity: string;
  seat_order: number;
}
