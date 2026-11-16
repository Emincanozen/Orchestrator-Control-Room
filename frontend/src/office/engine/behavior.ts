/**
 * Activity -> behaviour table. The ONLY input that decides what an agent does
 * is `agent.activity` (plus purely cosmetic local flags). Working animations
 * (typing, lit screens, testing, meeting talk) are produced only for working
 * activities.
 */
import type { AnimName, Indicator, OfficeAgent, ScreenMode } from './types';
import { ROOM_ORDER, isRoomId, type Layout, type RoomId, type Spot } from './layout';

export const ACTIVITIES = [
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

export const WORKING_ACTIVITIES: ReadonlySet<string> = new Set(['planning', 'coding', 'researching', 'testing', 'reviewing', 'consulting']);

export type SpotTarget = 'seat' | 'testing' | 'bookshelf' | 'meeting' | 'waiting' | 'stay';

export interface Behavior {
  activity: string;
  /** where the agent should be */
  spot: SpotTarget;
  /** animation once arrived (while walking the walk animation plays) */
  anim: AnimName;
  /** the screen at the agent's own desk while they sit there */
  screen: ScreenMode;
  /** overlay shown as soon as the activity is recorded (attention states) */
  indicator: Indicator | null;
  /** true for activities that show a working animation */
  working: boolean;
  dim: boolean;
  /** celebration is transient: after this many seconds fall back to idle */
  transientSeconds?: number;
}

export interface BehaviorContext {
  homeRoom: RoomId | null;
  /** cosmetic idle stroll to the waiting area (only honoured for `idle`) */
  strolling?: boolean;
}

export const CELEBRATE_SECONDS = 6;

export function behaviorFor(activity: string, ctx: BehaviorContext): Behavior {
  const base = { activity, screen: 'off' as ScreenMode, indicator: null as Indicator | null, working: false, dim: false };
  switch (activity) {
    case 'planning':
      return { ...base, spot: 'seat', anim: 'read', screen: 'doc', working: true };
    case 'coding':
      return { ...base, spot: 'seat', anim: 'type', screen: 'code', working: true };
    case 'researching':
      return ctx.homeRoom === 'research'
        ? { ...base, spot: 'bookshelf', anim: 'read', working: true }
        : { ...base, spot: 'seat', anim: 'read', screen: 'doc', working: true };
    case 'testing':
      return { ...base, spot: 'testing', anim: 'test', working: true };
    case 'reviewing':
      return { ...base, spot: 'seat', anim: 'review', screen: 'review', working: true };
    case 'consulting':
      return { ...base, spot: 'meeting', anim: 'talk', working: true, indicator: 'speech' };
    case 'rate_limited':
      return { ...base, spot: 'stay', anim: 'idle', indicator: 'warning' };
    case 'needs_approval':
      return { ...base, spot: 'stay', anim: 'idle', indicator: 'approval' };
    case 'blocked':
      return { ...base, spot: 'stay', anim: 'idle', indicator: 'blocked' };
    case 'celebrating':
      return { ...base, spot: 'seat', anim: 'celebrate', transientSeconds: CELEBRATE_SECONDS };
    case 'offline':
      return { ...base, spot: 'seat', anim: 'sleep', indicator: 'sleep', dim: true };
    case 'idle':
    default:
      // unknown activities are treated as idle: never as working
      return { ...base, spot: ctx.strolling ? 'waiting' : 'seat', anim: 'idle', activity };
  }
}

/** Room order used for deterministic ordering of agents. */
export function roomRank(room: string): number {
  const i = ROOM_ORDER.indexOf(room as RoomId);
  return i < 0 ? ROOM_ORDER.length : i;
}

export function homeRoomOf(agent: Pick<OfficeAgent, 'room'>): RoomId | null {
  return isRoomId(agent.room) ? agent.room : null;
}

export function compareAgents(a: OfficeAgent, b: OfficeAgent): number {
  return roomRank(a.room) - roomRank(b.room) || a.seat_order - b.seat_order || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

// ---------------------------------------------------------------------------
// Seat assignment

/**
 * Seats agents by `room` and `seat_order`. Agents of an unknown room, or in
 * excess of their room's seats, get a standing overflow spot in the Waiting Area.
 */
export function assignSeats(layout: Layout, agents: OfficeAgent[]): Map<string, Spot> {
  const out = new Map<string, Spot>();
  const used = new Map<RoomId, number>();
  let overflow = 0;
  for (const a of [...agents].sort(compareAgents)) {
    const room = homeRoomOf(a);
    const seats = room ? layout.roomById[room].seats : [];
    const n = room ? (used.get(room) ?? 0) : 0;
    if (room && n < seats.length) {
      out.set(a.id, seats[n]);
      used.set(room, n + 1);
    } else {
      out.set(a.id, layout.overflowSpots[overflow % layout.overflowSpots.length]);
      overflow++;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Labels

export interface LabelText {
  name: string;
  /** exact provider/model strings from the agent row; null when both are unknown */
  provider: string | null;
  model: string | null;
  /** `provider · model` style line, or null: nothing is invented */
  line: string | null;
}

export function labelFor(agent: Pick<OfficeAgent, 'name' | 'provider' | 'model'>): LabelText {
  const provider = agent.provider && agent.provider.length > 0 ? agent.provider : null;
  const model = agent.model && agent.model.length > 0 ? agent.model : null;
  const line = provider && model ? `${provider} · ${model}` : (provider ?? model);
  return { name: agent.name, provider, model, line };
}
