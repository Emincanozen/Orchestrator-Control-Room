/**
 * Office simulation: pure, deterministic, Pixi-free.
 * Consumes agent rows, owns positions/paths/animation clocks, and produces a
 * flat render list. The simulation never reads anything but the agent rows it
 * is given: no network, no store.
 */
import { appearanceFor, hashString, rng, type Appearance } from './appearance';
import { frameFor, jumpOffset } from './animation';
import { assignSeats, behaviorFor, homeRoomOf, type Behavior } from './behavior';
import { LAYOUT, type Layout, type Spot } from './layout';
import { findPath } from './pathfinding';
import { TILE, type AnimName, type Dir, type Indicator, type OfficeAgent, type Pose, type ScreenMode, type Tile } from './types';

export const WALK_SPEED = 48; // px per second (3 tiles/s)

const tileCenter = (t: Tile) => ({ x: t.x * TILE + TILE / 2, y: t.y * TILE + TILE - 1 });

export interface AgentRender {
  id: string;
  x: number;
  y: number;
  dir: Dir;
  pose: Pose;
  anim: AnimName;
  frame: number;
  /** extra vertical offset in px (celebration jump) */
  jump: number;
  indicator: Indicator | null;
  dim: boolean;
  moving: boolean;
  celebrating: boolean;
}

interface SimAgent {
  id: string;
  src: OfficeAgent;
  appearance: Appearance;
  behavior: Behavior;
  seat: Spot;
  tile: Tile;
  x: number;
  y: number;
  path: Tile[];
  dir: Dir;
  target: Spot | null;
  animT: number;
  lastAnim: AnimName;
  activitySince: number;
  strolling: boolean;
  nextStrollToggle: number;
  strollCycle: number;
  strollEligible: boolean;
}

export interface SimOptions {
  layout?: Layout;
  reducedMotion?: boolean;
}

export class OfficeSim {
  readonly layout: Layout;
  reducedMotion: boolean;
  time = 0;
  private agents = new Map<string, SimAgent>();
  private claims = new Map<string, string>(); // spot id -> agent id
  private seats = new Map<string, Spot>();
  private spotByTile = new Map<string, Spot>();

  constructor(opts: SimOptions = {}) {
    this.layout = opts.layout ?? LAYOUT;
    this.reducedMotion = opts.reducedMotion ?? false;
    for (const s of this.layout.spots) this.spotByTile.set(`${s.tile.x},${s.tile.y}`, s);
  }

  get size(): number {
    return this.agents.size;
  }

  appearanceOf(id: string): Appearance | undefined {
    return this.agents.get(id)?.appearance;
  }

  seatOf(id: string): Spot | undefined {
    return this.agents.get(id)?.seat;
  }

  setReducedMotion(v: boolean): void {
    this.reducedMotion = v;
    if (v) {
      for (const a of this.agents.values()) {
        a.strolling = false;
        this.retarget(a, true);
      }
    }
  }

  /** Reconcile with the latest agent rows (any order). */
  sync(list: OfficeAgent[]): void {
    this.seats = assignSeats(this.layout, list);
    const seen = new Set<string>();
    for (const src of list) {
      seen.add(src.id);
      let a = this.agents.get(src.id);
      const seat = this.seats.get(src.id)!;
      if (!a) {
        const r = rng(hashString(src.id) ^ 0x5eed);
        const t = seat.tile;
        const c = tileCenter(t);
        a = {
          id: src.id,
          src,
          appearance: appearanceFor(src.id, src.role),
          behavior: behaviorFor(src.activity, { homeRoom: homeRoomOf(src) }),
          seat,
          tile: { ...t },
          x: c.x,
          y: c.y,
          path: [],
          dir: seat.face,
          target: null,
          animT: 0,
          lastAnim: 'idle',
          activitySince: this.time,
          strolling: false,
          nextStrollToggle: this.time + 4 + r() * 14,
          strollCycle: 0,
          strollEligible: hashString(src.id) % 2 === 0,
        };
        this.agents.set(src.id, a);
        this.applyBehavior(a, src, true);
        continue;
      }
      const activityChanged = a.src.activity !== src.activity;
      a.src = src;
      a.seat = seat;
      if (activityChanged) {
        a.strolling = false;
        a.activitySince = this.time;
        a.nextStrollToggle = this.time + 6 + rng(hashString(src.id) + a.strollCycle)() * 10;
      }
      this.applyBehavior(a, src, false);
    }
    for (const id of [...this.agents.keys()]) {
      if (!seen.has(id)) {
        this.releaseClaims(id);
        this.agents.delete(id);
      }
    }
  }

  private applyBehavior(a: SimAgent, src: OfficeAgent, initial: boolean): void {
    a.behavior = behaviorFor(src.activity, { homeRoom: homeRoomOf(src), strolling: a.strolling });
    this.retarget(a, initial);
  }

  private releaseClaims(id: string): void {
    for (const [k, v] of this.claims) if (v === id) this.claims.delete(k);
  }

  private claim(a: SimAgent, spots: Spot[]): Spot {
    for (const s of spots) if (this.claims.get(s.id) === a.id) return s;
    this.releaseClaims(a.id);
    const free = spots.find((s) => !this.claims.has(s.id));
    const chosen = free ?? spots[hashString(a.id) % spots.length];
    if (free) this.claims.set(free.id, a.id);
    return chosen;
  }

  private targetSpot(a: SimAgent): Spot | null {
    const L = this.layout;
    const keepClaim = (spots: Spot[]) => this.claim(a, spots);
    switch (a.behavior.spot) {
      case 'seat':
        this.releaseClaims(a.id);
        return a.seat;
      case 'testing':
        return keepClaim(L.testingSpots);
      case 'bookshelf':
        return keepClaim(L.bookshelfSpots);
      case 'meeting':
        return keepClaim(L.meetingSpots);
      case 'waiting':
        return keepClaim(L.waitingSpots);
      case 'stay':
        this.releaseClaims(a.id);
        return null;
    }
  }

  /** Re-plan the path when the desired spot changed (also mid-walk). */
  private retarget(a: SimAgent, snap: boolean): void {
    const target = this.targetSpot(a);
    if ((target?.id ?? null) === (a.target?.id ?? null) && !snap) return;
    a.target = target;
    if (!target) {
      // stay in place: finish the tile we are heading to, then stop
      a.path = a.path.length ? [a.path[0]] : [];
      return;
    }
    if (snap || this.reducedMotion) {
      a.path = [];
      a.tile = { ...target.tile };
      const c = tileCenter(a.tile);
      a.x = c.x;
      a.y = c.y;
      a.dir = target.face;
      return;
    }
    const from = a.path.length ? a.path[0] : a.tile;
    const rest = findPath(this.layout, from, target.tile);
    if (rest === null) {
      a.path = a.path.length ? [a.path[0]] : [];
      return;
    }
    a.path = a.path.length ? [a.path[0], ...rest] : rest;
  }

  update(dt: number): void {
    this.time += dt;
    for (const a of this.agents.values()) {
      this.updateStroll(a);
      this.move(a, dt);
      a.animT += dt;
    }
  }

  private updateStroll(a: SimAgent): void {
    if (this.reducedMotion || a.src.activity !== 'idle' || !a.strollEligible) return;
    if (this.time < a.nextStrollToggle) return;
    a.strollCycle++;
    const r = rng(hashString(a.id) + a.strollCycle * 7919)();
    a.strolling = !a.strolling;
    a.nextStrollToggle = this.time + (a.strolling ? 7 + r * 8 : 16 + r * 24);
    this.applyBehavior(a, a.src, false);
  }

  private move(a: SimAgent, dt: number): void {
    let budget = WALK_SPEED * dt;
    while (budget > 0 && a.path.length) {
      const next = a.path[0];
      const c = tileCenter(next);
      const dx = c.x - a.x;
      const dy = c.y - a.y;
      const dist = Math.abs(dx) + Math.abs(dy);
      if (dist < 1e-6) {
        a.tile = next;
        a.path.shift();
        continue;
      }
      a.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : dy > 0 ? 'down' : 'up';
      if (dist <= budget) {
        a.x = c.x;
        a.y = c.y;
        budget -= dist;
        a.tile = next;
        a.path.shift();
      } else {
        a.x += (dx / dist) * budget;
        a.y += (dy / dist) * budget;
        budget = 0;
      }
    }
  }

  private arrived(a: SimAgent): boolean {
    if (a.path.length) return false;
    if (!a.target) return true;
    return a.target.tile.x === a.tile.x && a.target.tile.y === a.tile.y;
  }

  private effectiveAnim(a: SimAgent): AnimName {
    const b = a.behavior;
    if (b.transientSeconds !== undefined && this.time - a.activitySince > b.transientSeconds) return 'idle';
    return b.anim;
  }

  private render1(a: SimAgent): AgentRender {
    const moving = a.path.length > 0;
    const arrived = this.arrived(a);
    let anim: AnimName;
    let pose: Pose = 'stand';
    let dir = a.dir;
    if (moving) {
      anim = 'walk';
    } else {
      const here = a.target ?? this.spotByTile.get(`${a.tile.x},${a.tile.y}`) ?? null;
      const wantAnim = this.effectiveAnim(a);
      // a working animation is only shown at the place where it belongs
      anim = !arrived && wantAnim !== 'idle' && wantAnim !== 'sleep' ? 'idle' : wantAnim;
      if (here && here.tile.x === a.tile.x && here.tile.y === a.tile.y) {
        pose = here.pose;
        dir = here.face;
      } else dir = 'down';
      if (anim === 'celebrate') {
        pose = 'stand';
        dir = 'down';
      }
    }
    if (this.reducedMotion && anim === 'walk') anim = 'idle';
    const f = frameFor(anim, a.animT, this.reducedMotion);
    let indicator = a.behavior.indicator;
    if (indicator === 'speech' && (!arrived || anim !== 'talk')) indicator = null;
    return {
      id: a.id,
      x: a.x,
      y: a.y,
      dir,
      pose,
      anim,
      frame: f,
      jump: anim === 'celebrate' && !this.reducedMotion ? jumpOffset(f) : 0,
      indicator,
      dim: a.behavior.dim,
      moving,
      celebrating: anim === 'celebrate' && !this.reducedMotion,
    };
  }

  renders(): AgentRender[] {
    return [...this.agents.values()].map((a) => this.render1(a));
  }

  /** Screen modes of desks, keyed by seat spot id. A screen is lit only while its owner works there. */
  deskScreens(): Map<string, ScreenMode> {
    const m = new Map<string, ScreenMode>();
    for (const a of this.agents.values()) {
      const b = a.behavior;
      const atSeat = !a.path.length && a.tile.x === a.seat.tile.x && a.tile.y === a.seat.tile.y;
      const lit = atSeat && b.working && b.spot === 'seat' && b.screen !== 'off';
      m.set(a.seat.id, lit ? b.screen : 'off');
    }
    return m;
  }

  /** True while at least one agent is operating the testing workstation. */
  stationActive(): boolean {
    for (const a of this.agents.values()) {
      if (a.behavior.spot === 'testing' && this.arrived(a) && a.target && a.tile.x === a.target.tile.x && a.tile.y === a.target.tile.y) return true;
    }
    return false;
  }

  /** Test hook: the agent's current tile. */
  tileOf(id: string): Tile | undefined {
    const a = this.agents.get(id);
    return a ? { ...a.tile } : undefined;
  }

  pathOf(id: string): Tile[] {
    return this.agents.get(id)?.path.map((t) => ({ ...t })) ?? [];
  }
}
