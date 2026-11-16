/**
 * Office tile map, defined as data. 16px tiles, 33x22 tiles.
 *
 *   row 0        outer wall (north faces of the top rooms)
 *   rows 1-8     top rooms     : Leadership | Backend | Frontend | Research
 *   row 9        wall with doors (south faces look into the corridor)
 *   rows 10-11   corridor
 *   row 12       wall with doors (north faces of the bottom rooms)
 *   rows 13-20   bottom rooms  : Testing | Visual | Meeting | Waiting
 *   row 21       outer wall
 *
 * Every room interior is 7x8 tiles; room `i` starts at x = 1 + 8*i.
 */
import type { Dir, Tile } from './types';

export type RoomId = 'leadership' | 'backend' | 'frontend' | 'research' | 'testing' | 'visual' | 'meeting' | 'waiting';
export const ROOM_ORDER: RoomId[] = ['leadership', 'backend', 'frontend', 'research', 'testing', 'visual', 'meeting', 'waiting'];

export type FloorStyle =
  | 'wood-dark'
  | 'tile-blue'
  | 'carpet-teal'
  | 'wood-green'
  | 'concrete'
  | 'carpet-violet'
  | 'wood-warm'
  | 'tile-cream'
  | 'corridor';

export type FurnitureKind =
  | 'desk'
  | 'chair'
  | 'plant'
  | 'cabinet'
  | 'rack'
  | 'testbench'
  | 'bookshelf'
  | 'readtable'
  | 'meetingtable'
  | 'easel'
  | 'sofa'
  | 'coffee'
  | 'cooler';

export type SpotKind = 'seat' | 'testing' | 'bookshelf' | 'meeting' | 'waiting' | 'overflow';

export interface Spot {
  id: string;
  kind: SpotKind;
  room: RoomId;
  tile: Tile;
  face: Dir;
  pose: 'sit' | 'stand';
}

export interface FurnitureItem {
  id: string;
  kind: FurnitureKind;
  room: RoomId | 'corridor';
  x: number;
  y: number;
  /** footprint in tiles */
  w: number;
  h: number;
  blocks: boolean;
  face?: Dir;
  variant?: string;
  /** for desks: the seat spot whose occupant controls the screen */
  seatId?: string;
}

export interface Rug {
  x: number;
  y: number;
  w: number;
  h: number;
  style: 'blue' | 'red' | 'green' | 'violet' | 'sand';
}

export type WallDecorKind = 'window' | 'clock' | 'poster' | 'board' | 'plate';
export interface WallDecor {
  kind: WallDecorKind;
  x: number; // tile
  y: number; // wall row
  room: RoomId | 'corridor';
  label?: string;
}

export type TileKind = 'wall' | 'floor' | 'door';
export interface TileInfo {
  kind: TileKind;
  room: RoomId | 'corridor' | null;
  /** wall tile whose south neighbour is not a wall: draw its front face */
  face: boolean;
}

export interface RoomDef {
  id: RoomId;
  name: string;
  x0: number;
  y0: number;
  w: number;
  h: number;
  floor: FloorStyle;
  accent: number;
  door: Tile;
  seats: Spot[];
}

export interface Layout {
  cols: number;
  rows: number;
  rooms: RoomDef[];
  roomById: Record<RoomId, RoomDef>;
  tiles: TileInfo[][];
  furniture: FurnitureItem[];
  rugs: Rug[];
  decor: WallDecor[];
  spots: Spot[];
  walkable: boolean[][];
  /** extra path cost per tile (chairs, sofas: passable but discouraged) */
  cost: number[][];
  testingSpots: Spot[];
  bookshelfSpots: Spot[];
  meetingSpots: Spot[];
  waitingSpots: Spot[];
  overflowSpots: Spot[];
}

const ROOM_W = 7;
const ROOM_H = 8;

type FurnitureSpec = [FurnitureKind, number, number, number, number, Partial<FurnitureItem>?];

interface RoomSpec {
  id: RoomId;
  name: string;
  floor: FloorStyle;
  accent: number;
  rugs: [number, number, number, number, Rug['style']][];
  furniture: FurnitureSpec[];
  /** desks with a seat: [desk lx, desk ly, variant?]; the chair is at (lx, ly+1) */
  desks: [number, number, string?][];
  decor: [number, WallDecorKind][];
}

const SPECS: RoomSpec[] = [
  {
    id: 'leadership',
    name: 'LEADERSHIP OFFICE',
    floor: 'wood-dark',
    accent: 0xe0b34a,
    rugs: [[2, 3, 3, 3, 'red']],
    desks: [
      [1, 1],
      [4, 1],
    ],
    furniture: [
      ['plant', 6, 1, 1, 1],
      ['plant', 0, 6, 1, 1],
      ['cabinet', 6, 6, 1, 1],
    ],
    decor: [
      [0, 'window'],
      [6, 'clock'],
    ],
  },
  {
    id: 'backend',
    name: 'BACKEND DEVELOPMENT',
    floor: 'tile-blue',
    accent: 0x4f8fe0,
    rugs: [[2, 4, 3, 2, 'blue']],
    desks: [
      [1, 1],
      [4, 1],
    ],
    furniture: [
      ['rack', 6, 1, 1, 1],
      ['rack', 6, 2, 1, 1],
      ['plant', 0, 6, 1, 1],
      ['cabinet', 6, 6, 1, 1],
    ],
    decor: [
      [0, 'window'],
      [6, 'poster'],
    ],
  },
  {
    id: 'frontend',
    name: 'FRONTEND DEVELOPMENT',
    floor: 'carpet-teal',
    accent: 0x36c2a6,
    rugs: [[2, 4, 3, 3, 'violet']],
    desks: [
      [1, 1],
      [4, 1],
    ],
    furniture: [
      ['plant', 6, 1, 1, 1],
      ['plant', 0, 6, 1, 1],
      ['cabinet', 6, 6, 1, 1],
    ],
    decor: [
      [0, 'poster'],
      [6, 'window'],
    ],
  },
  {
    id: 'research',
    name: 'RESEARCH',
    floor: 'wood-green',
    accent: 0x58b86a,
    rugs: [[3, 5, 3, 2, 'green']],
    desks: [[1, 3]],
    furniture: [
      ['bookshelf', 0, 0, 2, 1],
      ['bookshelf', 5, 0, 2, 1],
      ['readtable', 4, 3, 2, 1],
      ['plant', 6, 6, 1, 1],
    ],
    decor: [],
  },
  {
    id: 'testing',
    name: 'TESTING',
    floor: 'concrete',
    accent: 0xf0a640,
    rugs: [],
    desks: [[1, 1]],
    furniture: [
      ['testbench', 4, 1, 3, 1],
      ['rack', 6, 4, 1, 1],
      ['rack', 6, 5, 1, 1],
      ['plant', 0, 6, 1, 1],
      ['cabinet', 3, 6, 1, 1],
    ],
    decor: [
      [0, 'window'],
      [6, 'board'],
    ],
  },
  {
    id: 'visual',
    name: 'VISUAL DESIGN',
    floor: 'carpet-violet',
    accent: 0xd66fb0,
    rugs: [[2, 4, 3, 3, 'sand']],
    desks: [[1, 1, 'tablet']],
    furniture: [
      ['easel', 4, 1, 1, 1],
      ['easel', 6, 1, 1, 1, { variant: 'b' }],
      ['plant', 0, 6, 1, 1],
      ['cabinet', 6, 6, 1, 1],
    ],
    decor: [
      [0, 'poster'],
      [6, 'window'],
    ],
  },
  {
    id: 'meeting',
    name: 'MEETING ROOM',
    floor: 'wood-warm',
    accent: 0xb08cf0,
    rugs: [[1, 2, 5, 4, 'blue']],
    desks: [[5, 6]],
    furniture: [
      ['meetingtable', 2, 3, 3, 2],
      ['plant', 0, 6, 1, 1],
    ],
    decor: [
      [0, 'window'],
      [6, 'board'],
    ],
  },
  {
    id: 'waiting',
    name: 'WAITING AREA',
    floor: 'tile-cream',
    accent: 0x6fd0d8,
    rugs: [[1, 3, 4, 3, 'sand']],
    desks: [],
    furniture: [
      ['sofa', 1, 1, 3, 1],
      ['coffee', 6, 1, 1, 1],
      ['cooler', 5, 1, 1, 1],
      ['plant', 0, 6, 1, 1],
      ['plant', 6, 6, 1, 1],
    ],
    decor: [
      [0, 'window'],
      [6, 'clock'],
    ],
  },
];

export function buildLayout(): Layout {
  const cols = 33;
  const rows = 22;
  const tiles: TileInfo[][] = Array.from({ length: rows }, () =>
    Array.from({ length: cols }, () => ({ kind: 'wall' as TileKind, room: null, face: false })),
  );
  const furniture: FurnitureItem[] = [];
  const rugs: Rug[] = [];
  const decor: WallDecor[] = [];
  const rooms: RoomDef[] = [];
  const spots: Spot[] = [];

  // corridor
  for (let y = 10; y <= 11; y++) for (let x = 1; x <= 31; x++) tiles[y][x] = { kind: 'floor', room: 'corridor', face: false };

  const testingSpots: Spot[] = [];
  const bookshelfSpots: Spot[] = [];
  const meetingSpots: Spot[] = [];
  const waitingSpots: Spot[] = [];

  SPECS.forEach((spec, idx) => {
    const top = idx < 4;
    const x0 = 1 + 8 * (idx % 4);
    const y0 = top ? 1 : 13;
    const abs = (lx: number, ly: number): Tile => ({ x: x0 + lx, y: y0 + ly });
    for (let ly = 0; ly < ROOM_H; ly++) for (let lx = 0; lx < ROOM_W; lx++) tiles[y0 + ly][x0 + lx] = { kind: 'floor', room: spec.id, face: false };
    const door = { x: x0 + 3, y: top ? y0 + ROOM_H : y0 - 1 };
    tiles[door.y][door.x] = { kind: 'door', room: spec.id, face: false };

    const seats: Spot[] = [];
    spec.desks.forEach(([lx, ly, variant], n) => {
      const seat: Spot = { id: `seat:${spec.id}:${n}`, kind: 'seat', room: spec.id, tile: abs(lx, ly + 1), face: 'up', pose: 'sit' };
      seats.push(seat);
      const d = abs(lx, ly);
      furniture.push({ id: `desk:${spec.id}:${n}`, kind: 'desk', room: spec.id, x: d.x, y: d.y, w: 2, h: 1, blocks: true, variant, seatId: seat.id });
      furniture.push({ id: `chair:${spec.id}:${n}`, kind: 'chair', room: spec.id, x: seat.tile.x, y: seat.tile.y, w: 1, h: 1, blocks: false, face: 'up' });
    });
    spots.push(...seats);

    spec.furniture.forEach(([kind, lx, ly, w, h, extra], n) => {
      const p = abs(lx, ly);
      furniture.push({ id: `${kind}:${spec.id}:${n}`, kind, room: spec.id, x: p.x, y: p.y, w, h, blocks: kind !== 'sofa', ...extra });
    });
    spec.rugs.forEach(([lx, ly, w, h, style]) => {
      const p = abs(lx, ly);
      rugs.push({ x: p.x, y: p.y, w, h, style });
    });

    // wall decor on the north wall of the room
    const wallY = y0 - 1;
    decor.push({ kind: 'plate', x: x0 + 1, y: wallY, room: spec.id, label: spec.name });
    spec.decor.forEach(([lx, kind]) => decor.push({ kind, x: x0 + lx, y: wallY, room: spec.id }));

    const mk = (kind: SpotKind, lx: number, ly: number, face: Dir, pose: 'sit' | 'stand', n: number): Spot => {
      const s: Spot = { id: `${kind}:${spec.id}:${n}`, kind, room: spec.id, tile: abs(lx, ly), face, pose };
      spots.push(s);
      return s;
    };
    if (spec.id === 'testing') [4, 5, 6].forEach((lx, n) => testingSpots.push(mk('testing', lx, 2, 'up', 'stand', n)));
    if (spec.id === 'research') [1, 5].forEach((lx, n) => bookshelfSpots.push(mk('bookshelf', lx, 1, 'up', 'stand', n)));
    if (spec.id === 'meeting') {
      const ms: [number, number, Dir][] = [
        [2, 2, 'down'],
        [3, 2, 'down'],
        [4, 2, 'down'],
        [2, 5, 'up'],
        [3, 5, 'up'],
        [4, 5, 'up'],
        [1, 3, 'right'],
        [5, 3, 'left'],
        [1, 4, 'right'],
        [5, 4, 'left'],
      ];
      ms.forEach(([lx, ly, face], n) => {
        meetingSpots.push(mk('meeting', lx, ly, face, 'sit', n));
        const p = abs(lx, ly);
        furniture.push({ id: `chair:meeting:t${n}`, kind: 'chair', room: 'meeting', x: p.x, y: p.y, w: 1, h: 1, blocks: false, face });
      });
    }
    if (spec.id === 'waiting') {
      waitingSpots.push(mk('waiting', 1, 1, 'down', 'sit', 0));
      waitingSpots.push(mk('waiting', 3, 1, 'down', 'sit', 1));
      waitingSpots.push(mk('waiting', 6, 2, 'up', 'stand', 2));
      waitingSpots.push(mk('waiting', 4, 4, 'left', 'stand', 3));
      waitingSpots.push(mk('waiting', 3, 4, 'right', 'stand', 4));
    }
    rooms.push({ id: spec.id, name: spec.name, x0, y0, w: ROOM_W, h: ROOM_H, floor: spec.floor, accent: spec.accent, door, seats });
  });

  // overflow standing spots in the waiting room (unknown room / more agents than seats)
  const waiting = rooms.find((r) => r.id === 'waiting')!;
  const overflowSpots: Spot[] = (
    [
      [2, 6],
      [3, 6],
      [4, 6],
      [2, 5],
      [4, 5],
      [5, 5],
      [1, 6],
      [5, 6],
    ] as [number, number][]
  ).map(([lx, ly], n) => ({
    id: `overflow:waiting:${n}`,
    kind: 'overflow' as SpotKind,
    room: 'waiting' as RoomId,
    tile: { x: waiting.x0 + lx, y: waiting.y0 + ly },
    face: 'down' as Dir,
    pose: 'stand' as const,
  }));
  spots.push(...overflowSpots);

  // corridor furniture and decor
  furniture.push({ id: 'plant:corridor:0', kind: 'plant', room: 'corridor', x: 1, y: 10, w: 1, h: 1, blocks: true });
  furniture.push({ id: 'plant:corridor:1', kind: 'plant', room: 'corridor', x: 31, y: 10, w: 1, h: 1, blocks: true });
  decor.push({ kind: 'clock', x: 16, y: 9, room: 'corridor' });
  decor.push({ kind: 'poster', x: 8, y: 9, room: 'corridor' });
  decor.push({ kind: 'poster', x: 24, y: 9, room: 'corridor' });

  // wall faces: wall tiles with a non-wall tile to the south
  for (let y = 0; y < rows - 1; y++) {
    for (let x = 0; x < cols; x++) {
      const t = tiles[y][x];
      if (t.kind === 'wall' && tiles[y + 1][x].kind !== 'wall') t.face = true;
    }
  }

  const walkable = tiles.map((row) => row.map((t) => t.kind !== 'wall'));
  const cost = tiles.map((row) => row.map(() => 1));
  for (const f of furniture) {
    for (let dy = 0; dy < f.h; dy++) {
      for (let dx = 0; dx < f.w; dx++) {
        if (f.blocks) walkable[f.y + dy][f.x + dx] = false;
        else cost[f.y + dy][f.x + dx] = 4;
      }
    }
  }

  const roomById = Object.fromEntries(rooms.map((r) => [r.id, r])) as Record<RoomId, RoomDef>;
  return { cols, rows, rooms, roomById, tiles, furniture, rugs, decor, spots, walkable, cost, testingSpots, bookshelfSpots, meetingSpots, waitingSpots, overflowSpots };
}

export const LAYOUT: Layout = buildLayout();

export function isRoomId(v: string): v is RoomId {
  return (ROOM_ORDER as string[]).includes(v);
}
