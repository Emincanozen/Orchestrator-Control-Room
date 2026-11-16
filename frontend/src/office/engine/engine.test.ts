import { describe, expect, it } from 'vitest';
import { appearanceFor } from './appearance';
import { frameFor } from './animation';
import { ACTIVITIES, assignSeats, behaviorFor, labelFor, WORKING_ACTIVITIES } from './behavior';
import { LAYOUT } from './layout';
import { findPath, type PathGrid } from './pathfinding';
import { OfficeSim } from './sim';
import type { OfficeAgent } from './types';

const mk = (o: Partial<OfficeAgent> = {}): OfficeAgent => ({
  id: 'a1',
  name: 'Ada',
  role: 'backend',
  room: 'backend',
  provider: 'claude',
  model: 'claude-sonnet',
  activity: 'idle',
  seat_order: 0,
  ...o,
});

const grid = (rows: string[]): PathGrid => ({
  cols: rows[0].length,
  rows: rows.length,
  walkable: rows.map((r) => [...r].map((c) => c !== '#')),
});

describe('pathfinding', () => {
  it('routes through a door and never crosses blocked tiles', () => {
    const g = grid(['.....', '##.##', '.....']);
    const p = findPath(g, { x: 0, y: 0 }, { x: 4, y: 2 })!;
    expect(p).not.toBeNull();
    expect(p.some((t) => t.x === 2 && t.y === 1)).toBe(true);
    for (const t of p) expect(g.walkable[t.y][t.x]).toBe(true);
    expect(p[p.length - 1]).toEqual({ x: 4, y: 2 });
  });
  it('returns null when unreachable or the target is blocked', () => {
    expect(findPath(grid(['..#..', '..#..']), { x: 0, y: 0 }, { x: 4, y: 0 })).toBeNull();
    expect(findPath(grid(['.#.']), { x: 0, y: 0 }, { x: 1, y: 0 })).toBeNull();
  });
  it('returns an empty path for from === to', () => {
    expect(findPath(grid(['..']), { x: 0, y: 0 }, { x: 0, y: 0 })).toEqual([]);
  });
  it('every spot of the office is reachable from every seat', () => {
    const origin = LAYOUT.roomById.leadership.seats[0].tile;
    for (const s of LAYOUT.spots) {
      expect(LAYOUT.walkable[s.tile.y][s.tile.x]).toBe(true);
      expect(findPath(LAYOUT, origin, s.tile)).not.toBeNull();
    }
  });
});

describe('behaviour state machine', () => {
  it('defines a behaviour for every AgentActivity, with working animations only for working activities', () => {
    const workingAnims = new Set(['type', 'read', 'review', 'test', 'talk']);
    for (const act of ACTIVITIES) {
      const b = behaviorFor(act, { homeRoom: 'backend' });
      expect(b.working).toBe(WORKING_ACTIVITIES.has(act));
      if (!b.working) {
        expect(workingAnims.has(b.anim)).toBe(false);
        expect(b.screen).toBe('off');
        expect(['testing', 'meeting', 'bookshelf']).not.toContain(b.spot);
      }
    }
  });
  it('maps activities to targets and animations', () => {
    const t = (a: string, room: 'backend' | 'research' = 'backend') => behaviorFor(a, { homeRoom: room });
    expect(t('planning')).toMatchObject({ spot: 'seat', anim: 'read', screen: 'doc' });
    expect(t('coding')).toMatchObject({ spot: 'seat', anim: 'type', screen: 'code' });
    expect(t('researching')).toMatchObject({ spot: 'seat', anim: 'read' });
    expect(t('researching', 'research')).toMatchObject({ spot: 'bookshelf', anim: 'read' });
    expect(t('testing')).toMatchObject({ spot: 'testing', anim: 'test' });
    expect(t('reviewing')).toMatchObject({ spot: 'seat', anim: 'review' });
    expect(t('consulting')).toMatchObject({ spot: 'meeting', anim: 'talk' });
    expect(t('rate_limited')).toMatchObject({ spot: 'stay', indicator: 'warning' });
    expect(t('needs_approval')).toMatchObject({ spot: 'stay', indicator: 'approval' });
    expect(t('blocked')).toMatchObject({ spot: 'stay', indicator: 'blocked' });
    expect(t('celebrating')).toMatchObject({ anim: 'celebrate' });
    expect(t('offline')).toMatchObject({ dim: true, indicator: 'sleep' });
  });
  it('idle strolls still read as not working; unknown activities are idle', () => {
    expect(behaviorFor('idle', { homeRoom: 'backend', strolling: true })).toMatchObject({ spot: 'waiting', anim: 'idle', screen: 'off', working: false });
    expect(behaviorFor('something_new', { homeRoom: null })).toMatchObject({ anim: 'idle', working: false });
  });
});

describe('seat assignment', () => {
  it('seats by room and seat_order, with overflow in the waiting area', () => {
    const agents = [
      mk({ id: 'b2', room: 'backend', seat_order: 1 }),
      mk({ id: 'b1', room: 'backend', seat_order: 0 }),
      mk({ id: 'b3', room: 'backend', seat_order: 2 }),
      mk({ id: 'x', room: 'unknown-room' }),
    ];
    const m = assignSeats(LAYOUT, agents);
    expect(m.get('b1')!.id).toBe('seat:backend:0');
    expect(m.get('b2')!.id).toBe('seat:backend:1');
    expect(m.get('b3')!.kind).toBe('overflow');
    expect(m.get('x')!.kind).toBe('overflow');
  });
});

describe('appearance', () => {
  it('is deterministic per id and varies between ids', () => {
    expect(appearanceFor('agt_x', 'backend')).toEqual(appearanceFor('agt_x', 'backend'));
    const keys = new Set(['agt_backend_1', 'agt_backend_2', 'agt_frontend_1', 'agt_frontend_2', 'agt_research', 'agt_test', 'agt_visual', 'agt_reviewer'].map((id) => appearanceFor(id, 'backend').key));
    expect(keys.size).toBeGreaterThanOrEqual(7);
  });
  it('makes the leader and advisor visually distinct', () => {
    expect(appearanceFor('a', 'leader').accessory).toBe('crown-tie');
    expect(appearanceFor('a', 'advisor').accessory).toBe('glasses-scarf');
    expect(appearanceFor('a', 'leader').key).not.toBe(appearanceFor('a', 'advisor').key);
  });
});

describe('labels', () => {
  it('never invents provider or model', () => {
    expect(labelFor({ name: 'A', provider: null, model: null })).toMatchObject({ provider: null, model: null, line: null });
    expect(labelFor({ name: 'A', provider: 'claude', model: null }).line).toBe('claude');
    expect(labelFor({ name: 'A', provider: null, model: 'm-1' }).line).toBe('m-1');
    expect(labelFor({ name: 'A', provider: 'claude', model: 'claude-sonnet-4' }).line).toBe('claude · claude-sonnet-4');
  });
});

describe('animation frames', () => {
  it('stays in range and is static under reduced motion', () => {
    for (let t = 0; t < 5; t += 0.07) expect(frameFor('walk', t)).toBeLessThan(4);
    expect(frameFor('walk', 3.3, true)).toBe(0);
  });
});

describe('simulation', () => {
  const run = (sim: OfficeSim, seconds: number) => {
    for (let i = 0; i < seconds * 20; i++) sim.update(0.05);
  };
  it('starts agents at their seat and shows no working animation while idle', () => {
    const sim = new OfficeSim();
    sim.sync([mk()]);
    const r = sim.renders()[0];
    expect(r.anim).toBe('idle');
    expect(r.pose).toBe('sit');
    expect(sim.deskScreens().get('seat:backend:0')).toBe('off');
  });
  it('walks to the testing workstation, then operates it; screens stay dark meanwhile', () => {
    const sim = new OfficeSim();
    sim.sync([mk({ activity: 'coding' })]);
    run(sim, 1);
    expect(sim.deskScreens().get('seat:backend:0')).toBe('code');
    sim.sync([mk({ activity: 'testing' })]);
    sim.update(0.1);
    expect(sim.renders()[0].anim).toBe('walk');
    expect(sim.deskScreens().get('seat:backend:0')).toBe('off');
    expect(sim.stationActive()).toBe(false);
    run(sim, 30);
    expect(sim.renders()[0].anim).toBe('test');
    expect(sim.stationActive()).toBe(true);
  });
  it('re-paths when the activity changes mid-walk and never steps on blocked tiles', () => {
    const sim = new OfficeSim();
    sim.sync([mk({ activity: 'consulting' })]);
    const seen: string[] = [];
    for (let i = 0; i < 20; i++) {
      sim.update(0.05);
      const t = sim.tileOf('a1')!;
      seen.push(`${t.x},${t.y}`);
    }
    sim.sync([mk({ activity: 'coding' })]);
    for (let i = 0; i < 1200; i++) {
      sim.update(0.05);
      const t = sim.tileOf('a1')!;
      expect(LAYOUT.walkable[t.y][t.x]).toBe(true);
    }
    expect(sim.tileOf('a1')).toEqual(LAYOUT.roomById.backend.seats[0].tile);
    expect(sim.renders()[0].anim).toBe('type');
  });
  it('stays in place for blocked and shows the indicator', () => {
    const sim = new OfficeSim();
    sim.sync([mk({ activity: 'blocked' })]);
    run(sim, 2);
    const r = sim.renders()[0];
    expect(r.indicator).toBe('blocked');
    expect(r.anim).toBe('idle');
  });
  it('celebrates, then falls back to idle', () => {
    const sim = new OfficeSim();
    sim.sync([mk({ activity: 'celebrating' })]);
    run(sim, 1);
    expect(sim.renders()[0].anim).toBe('celebrate');
    run(sim, 8);
    expect(sim.renders()[0].anim).toBe('idle');
  });
  it('reduced motion teleports instead of walking', () => {
    const sim = new OfficeSim({ reducedMotion: true });
    sim.sync([mk()]);
    sim.sync([mk({ activity: 'consulting' })]);
    const r = sim.renders()[0];
    expect(r.moving).toBe(false);
    expect(r.anim).toBe('talk');
  });
});
