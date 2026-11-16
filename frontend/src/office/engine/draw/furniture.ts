/**
 * Procedural furniture sprites. `furnitureSprite()` returns a small canvas for
 * (kind, variant, state). The renderer caches the resulting textures, so every
 * sprite is drawn exactly once.
 */
import type { Dir } from '../types';
import type { FurnitureItem, FurnitureKind } from '../layout';
import { PAL, shade } from './color';

type Ctx = CanvasRenderingContext2D;
const r = (c: Ctx, x: number, y: number, w: number, h: number, col: string) => {
  c.fillStyle = col;
  c.fillRect(x, y, w, h);
};

export interface SpriteSpec {
  w: number;
  h: number;
}

/** Sprite size in px. Sprites are anchored at the bottom-left of the item's footprint. */
export const SPRITE_SIZE: Record<FurnitureKind, SpriteSpec> = {
  desk: { w: 32, h: 27 },
  chair: { w: 16, h: 16 },
  plant: { w: 16, h: 28 },
  cabinet: { w: 16, h: 26 },
  rack: { w: 16, h: 30 },
  testbench: { w: 48, h: 32 },
  bookshelf: { w: 32, h: 34 },
  readtable: { w: 32, h: 24 },
  meetingtable: { w: 48, h: 37 },
  easel: { w: 16, h: 32 },
  sofa: { w: 48, h: 26 },
  coffee: { w: 16, h: 26 },
  cooler: { w: 16, h: 28 },
};

export type DeskMode = 'off' | 'code' | 'doc' | 'review' | 'art';

/** State key components for sprites that animate/switch. */
export interface SpriteState {
  /** screen mode / rack lit state */
  mode?: string;
  /** animation frame (0..n) */
  frame?: number;
}

export const spriteKey = (it: Pick<FurnitureItem, 'kind' | 'variant' | 'face' | 'room'>, st: SpriteState = {}): string =>
  `${it.kind}|${it.variant ?? ''}|${it.face ?? ''}|${it.room}|${st.mode ?? ''}|${st.frame ?? 0}`;

export function furnitureSprite(it: Pick<FurnitureItem, 'kind' | 'variant' | 'face' | 'room'>, st: SpriteState, make: (w: number, h: number) => HTMLCanvasElement): HTMLCanvasElement {
  const { w, h } = SPRITE_SIZE[it.kind];
  const cv = make(w, h);
  const c = cv.getContext('2d') as Ctx;
  switch (it.kind) {
    case 'desk':
      drawDesk(c, it.variant === 'tablet', (st.mode as DeskMode) ?? 'off', st.frame ?? 0);
      break;
    case 'chair':
      drawChair(c, it.face ?? 'up');
      break;
    case 'plant':
      drawPlant(c);
      break;
    case 'cabinet':
      drawCabinet(c);
      break;
    case 'rack':
      drawRack(c, it.room === 'testing', st.mode === 'lit', st.frame ?? 0);
      break;
    case 'testbench':
      drawTestBench(c, st.mode === 'lit', st.frame ?? 0);
      break;
    case 'bookshelf':
      drawBookshelf(c);
      break;
    case 'readtable':
      drawReadTable(c);
      break;
    case 'meetingtable':
      drawMeetingTable(c);
      break;
    case 'easel':
      drawEasel(c, it.variant === 'b');
      break;
    case 'sofa':
      drawSofa(c);
      break;
    case 'coffee':
      drawCoffee(c);
      break;
    case 'cooler':
      drawCooler(c);
      break;
  }
  return cv;
}

// ---------------------------------------------------------------------------

function screen(c: Ctx, x: number, y: number, w: number, h: number, mode: DeskMode, frame: number): void {
  if (mode === 'off') {
    r(c, x, y, w, h, PAL.screenOff);
    r(c, x + 1, y + 1, 3, 1, '#222c39');
    r(c, x + 2, y + 2, 2, 1, '#1a222d');
    return;
  }
  if (mode === 'code') {
    r(c, x, y, w, h, '#06231f');
    const cols = [PAL.phosphor, PAL.amber, '#6aa8ff', '#e8f6ff', '#f472b6'];
    for (let i = 0; i < h - 1; i += 2) {
      const row = i / 2 + frame;
      const len = 2 + ((row * 5 + 3) % (w - 4));
      const indent = (row * 3) % 3;
      r(c, x + 1 + indent, y + 1 + i, Math.min(len, w - 2 - indent), 1, cols[row % cols.length]);
    }
    if (frame % 2 === 0) r(c, x + w - 3, y + h - 2, 2, 1, '#e8f6ff');
    return;
  }
  if (mode === 'doc') {
    r(c, x, y, w, h, '#dfe7f0');
    r(c, x + 1, y + 1, Math.floor(w / 2), 1, '#38485e');
    for (let i = 3; i < h - 1; i += 2) r(c, x + 1, y + i, w - 2 - ((i + frame) % 3) * 2, 1, '#8c9bb0');
    return;
  }
  if (mode === 'review') {
    r(c, x, y, w, h, '#101b2a');
    for (let i = 0; i < h - 1; i += 2) {
      const ok = (i / 2 + frame) % 3 !== 0;
      r(c, x + 1, y + 1 + i, 2, 1, ok ? PAL.green : PAL.red);
      r(c, x + 4, y + 1 + i, w - 6 - ((i + frame) % 3), 1, '#8ea2bd');
    }
    return;
  }
  // art
  r(c, x, y, w, h, '#241b3a');
  r(c, x + 1, y + 1 + (frame % 2), 4, 3, '#f472b6');
  r(c, x + 5, y + 2, 4, 3, '#38e8c6');
  r(c, x + 3, y + h - 3, 6, 2, '#f5b94a');
}

function drawDesk(c: Ctx, tablet: boolean, mode: DeskMode, frame: number): void {
  const lit = mode !== 'off';
  const art = tablet && lit ? 'art' : mode;
  // monitor
  r(c, 2, 0, 14, 12, '#2a303c');
  r(c, 2, 0, 14, 1, '#3f4858');
  screen(c, 3, 1, 12, 9, art, frame);
  r(c, 3, 10, 12, 1, '#1d222c');
  r(c, 8, 12, 3, 2, '#3a4252');
  r(c, 6, 13, 7, 1, '#4b5566');
  // desk top
  r(c, 0, 14, 32, 13, PAL.wood);
  r(c, 0, 14, 32, 1, PAL.woodLight);
  r(c, 0, 15, 32, 1, shade(PAL.woodLight, 0.9));
  r(c, 0, 23, 32, 4, PAL.woodDark);
  r(c, 0, 23, 32, 1, shade(PAL.woodDark, 1.25));
  r(c, 1, 26, 3, 1, '#2d1d12');
  r(c, 28, 26, 3, 1, '#2d1d12');
  // keyboard
  r(c, 3, 17, 11, 4, '#c3ccd8');
  r(c, 3, 20, 11, 1, '#8c98a8');
  for (let i = 0; i < 5; i++) r(c, 4 + i * 2, 18, 1, 1, '#7c8797');
  // glow from a lit screen onto the desk
  if (lit) r(c, 2, 15, 13, 1, tablet ? '#b88ad8' : mode === 'doc' ? '#e8f0fa' : '#38e8c6');
  // right side: mug + papers or tablet
  if (tablet) {
    r(c, 18, 16, 10, 6, '#20262f');
    r(c, 19, 17, 8, 4, lit ? '#3a2f5a' : '#2b323d');
    if (lit) r(c, 21, 18, 3, 2, '#f472b6');
    r(c, 29, 16, 1, 5, '#e8b04a');
  } else {
    r(c, 17, 16, 7, 5, PAL.paper);
    r(c, 18, 17, 5, 1, PAL.paperShade);
    r(c, 18, 19, 4, 1, PAL.paperShade);
    r(c, 26, 16, 3, 4, '#d9d9e2');
    r(c, 26, 16, 3, 1, '#f1f1f7');
    r(c, 29, 17, 1, 2, '#d9d9e2');
  }
}

function drawChair(c: Ctx, face: Dir): void {
  const seat = '#3b4b69';
  const seatHi = '#4e6385';
  const back = '#2f3c55';
  const backHi = '#41537a';
  if (face === 'up') {
    r(c, 2, 5, 12, 8, seat);
    r(c, 2, 5, 12, 1, seatHi);
    r(c, 1, 10, 14, 5, back);
    r(c, 1, 10, 14, 1, backHi);
    r(c, 4, 15, 2, 1, '#1d2433');
    r(c, 10, 15, 2, 1, '#1d2433');
  } else if (face === 'down') {
    r(c, 1, 1, 14, 5, back);
    r(c, 1, 1, 14, 1, backHi);
    r(c, 2, 5, 12, 8, seat);
    r(c, 2, 6, 12, 1, seatHi);
    r(c, 4, 13, 2, 2, '#1d2433');
    r(c, 10, 13, 2, 2, '#1d2433');
  } else {
    const left = face === 'left';
    // sitter faces `face`; the backrest is on the opposite side
    r(c, left ? 3 : 1, 4, 12, 9, seat);
    r(c, left ? 3 : 1, 4, 12, 1, seatHi);
    r(c, left ? 11 : 1, 1, 4, 12, back);
    r(c, left ? 11 : 1, 1, 4, 1, backHi);
    r(c, 4, 14, 2, 1, '#1d2433');
    r(c, 10, 14, 2, 1, '#1d2433');
  }
}

function drawPlant(c: Ctx): void {
  r(c, 4, 19, 8, 8, PAL.pot);
  r(c, 3, 18, 10, 2, shade(PAL.pot, 1.2));
  r(c, 4, 25, 8, 2, PAL.potDark);
  r(c, 5, 20, 2, 5, shade(PAL.pot, 1.12));
  r(c, 7, 13, 2, 6, PAL.leafDark);
  const blob = (x: number, y: number, w: number, h: number) => {
    r(c, x, y, w, h, PAL.leaf);
    r(c, x + 1, y, Math.max(1, w - 3), 1, PAL.leafLight);
    r(c, x, y + h - 1, w, 1, PAL.leafDark);
  };
  blob(2, 7, 6, 5);
  blob(8, 6, 6, 5);
  blob(5, 1, 6, 6);
  blob(1, 12, 5, 4);
  blob(10, 12, 5, 4);
  blob(5, 9, 6, 5);
}

function drawCabinet(c: Ctx): void {
  r(c, 1, 5, 14, 21, '#5d6b82');
  r(c, 1, 5, 14, 2, '#7b8aa3');
  r(c, 1, 24, 14, 2, '#3c4659');
  for (const y of [8, 14, 20]) {
    r(c, 3, y, 10, 5, '#6f7f98');
    r(c, 3, y + 4, 10, 1, '#46526a');
    r(c, 6, y + 2, 4, 1, '#d9e2ec');
  }
  // things on top
  r(c, 3, 1, 4, 4, '#c0463f');
  r(c, 3, 1, 4, 1, '#e06a62');
  r(c, 8, 2, 5, 3, '#e9eef5');
}

function drawRack(c: Ctx, dynamic: boolean, lit: boolean, frame: number): void {
  r(c, 1, 2, 14, 28, '#1f2632');
  r(c, 1, 2, 14, 1, '#3a4658');
  r(c, 0, 3, 1, 26, '#2c3544');
  r(c, 15, 3, 1, 26, '#12161e');
  for (let i = 0; i < 5; i++) {
    const y = 5 + i * 5;
    r(c, 3, y, 10, 4, '#2c3646');
    r(c, 3, y, 10, 1, '#3d495c');
    r(c, 4, y + 2, 4, 1, '#151a23');
    // LEDs: static power LED for the backend racks, dynamic only while testing
    if (!dynamic) {
      r(c, 11, y + 1, 1, 1, i === 0 ? '#2a8a5a' : '#1c3a2c');
    } else if (lit) {
      const on = (i + frame) % 2 === 0;
      r(c, 10, y + 1, 1, 1, on ? PAL.phosphor : '#176b5c');
      r(c, 12, y + 1, 1, 1, !on ? PAL.amber : '#7a5a1e');
    } else {
      r(c, 10, y + 1, 1, 1, '#243040');
      r(c, 12, y + 1, 1, 1, '#243040');
    }
  }
}

function drawTestBench(c: Ctx, lit: boolean, frame: number): void {
  // two monitors on a workbench
  for (const x0 of [4, 28]) {
    r(c, x0, 1, 17, 13, '#2a303c');
    r(c, x0, 1, 17, 1, '#3f4858');
    if (lit) {
      r(c, x0 + 1, 2, 15, 10, '#0c1c24');
      // pass/fail bars
      for (let i = 0; i < 4; i++) {
        const bad = (i + frame + (x0 > 10 ? 1 : 0)) % 4 === 3;
        r(c, x0 + 2, 3 + i * 2, 2, 1, bad ? PAL.red : PAL.green);
        r(c, x0 + 5, 3 + i * 2, 5 + ((i * 3 + frame) % 6), 1, '#8ea2bd');
      }
      r(c, x0 + 11, 9, 4, 2, PAL.amber);
    } else {
      r(c, x0 + 1, 2, 15, 10, PAL.screenOff);
      r(c, x0 + 2, 3, 3, 1, '#222c39');
    }
    r(c, x0 + 6, 14, 5, 2, '#3a4252');
  }
  r(c, 0, 16, 48, 16, '#6b7a8f');
  r(c, 0, 16, 48, 1, '#9aa8bb');
  r(c, 0, 17, 48, 1, '#808fa4');
  r(c, 0, 27, 48, 5, '#3d4859');
  r(c, 0, 27, 48, 1, '#586579');
  // keyboards, a device under test
  r(c, 6, 19, 11, 4, '#c3ccd8');
  r(c, 30, 19, 11, 4, '#c3ccd8');
  r(c, 20, 19, 7, 5, '#222a36');
  r(c, 21, 20, 5, 3, lit ? PAL.phosphor : '#131820');
}

function drawBookshelf(c: Ctx): void {
  r(c, 0, 0, 32, 34, '#4e3320');
  r(c, 0, 0, 32, 2, '#7a5535');
  r(c, 1, 2, 30, 30, '#2f1f14');
  const palette = ['#c0463f', '#3f7fd9', '#e0a63a', '#2fb89a', '#8a63d2', '#d86fa8', '#e8e8ee', '#5fa84a'];
  for (let shelf = 0; shelf < 3; shelf++) {
    const y = 3 + shelf * 10;
    let x = 2;
    let i = shelf * 3;
    while (x < 29) {
      const w = 2 + ((i * 7) % 3);
      const h = 6 + ((i * 5) % 4);
      if (x + w > 30) break;
      if ((i * 11) % 9 === 0) {
        x += 2;
        i++;
        continue;
      }
      const col = palette[(i * 3 + shelf) % palette.length];
      r(c, x, y + (8 - h), w, h, col);
      r(c, x, y + (8 - h), w, 1, shade(col, 1.25));
      x += w;
      i++;
    }
    r(c, 1, y + 8, 30, 2, '#6a4a2e');
  }
  r(c, 0, 32, 32, 2, '#3a2515');
}

function drawReadTable(c: Ctx): void {
  r(c, 0, 8, 32, 16, '#8b6a46');
  r(c, 0, 8, 32, 2, '#b08c62');
  r(c, 0, 19, 32, 5, '#5f4630');
  // open books and a small lamp
  r(c, 3, 11, 9, 5, '#e9eef5');
  r(c, 7, 11, 1, 5, '#9aa7b8');
  r(c, 4, 12, 3, 1, '#9aa7b8');
  r(c, 9, 12, 2, 1, '#9aa7b8');
  r(c, 14, 12, 7, 4, '#3f7fd9');
  r(c, 14, 12, 7, 1, '#6aa2f0');
  r(c, 15, 14, 5, 1, '#e9eef5');
  r(c, 25, 4, 1, 10, '#8b98a9');
  r(c, 23, 2, 6, 3, '#f5b94a');
  r(c, 24, 5, 4, 1, '#fff0b8');
  r(c, 24, 13, 3, 2, '#4b5563');
}

function drawMeetingTable(c: Ctx): void {
  r(c, 0, 4, 48, 33, '#5a3d28');
  r(c, 0, 4, 48, 28, '#8e6644');
  r(c, 0, 4, 48, 2, '#b88a5e');
  r(c, 1, 6, 46, 1, '#a07650');
  r(c, 0, 29, 48, 3, '#6e4c31');
  // laptops, papers, mug, jug
  r(c, 5, 8, 8, 5, '#2a303c');
  r(c, 6, 9, 6, 3, '#38e8c6');
  r(c, 5, 13, 8, 2, '#c3ccd8');
  r(c, 35, 8, 8, 5, '#2a303c');
  r(c, 36, 9, 6, 3, '#6aa8ff');
  r(c, 35, 13, 8, 2, '#c3ccd8');
  r(c, 17, 11, 7, 5, PAL.paper);
  r(c, 18, 12, 5, 1, PAL.paperShade);
  r(c, 18, 14, 4, 1, PAL.paperShade);
  r(c, 26, 18, 7, 5, PAL.paper);
  r(c, 27, 19, 5, 1, PAL.paperShade);
  r(c, 22, 20, 3, 4, '#d9d9e2');
  r(c, 21, 22, 6, 1, '#d9d9e2');
  r(c, 8, 19, 3, 4, '#c0463f');
  r(c, 38, 20, 4, 6, '#8fc7e8');
  r(c, 38, 20, 4, 1, '#d8f0ff');
}

function drawEasel(c: Ctx, alt: boolean): void {
  // legs
  r(c, 3, 16, 2, 16, '#6b4a2e');
  r(c, 11, 16, 2, 16, '#6b4a2e');
  r(c, 7, 14, 2, 18, '#7b5636');
  // canvas
  r(c, 1, 1, 14, 16, '#e9eef5');
  r(c, 1, 1, 14, 1, '#ffffff');
  if (!alt) {
    r(c, 2, 2, 12, 10, '#7fc3e8');
    r(c, 2, 8, 12, 4, '#4ea866');
    r(c, 9, 3, 3, 3, '#f5d04a');
    r(c, 4, 6, 3, 3, '#2f7a45');
  } else {
    r(c, 2, 2, 12, 14, '#2d2250');
    r(c, 3, 3, 5, 5, '#f472b6');
    r(c, 8, 6, 5, 5, '#38e8c6');
    r(c, 4, 10, 6, 4, '#f5b94a');
  }
  r(c, 1, 17, 14, 2, '#6b4a2e');
  r(c, 12, 19, 3, 3, '#c0463f');
  r(c, 1, 17, 14, 1, '#8a6240');
}

function drawSofa(c: Ctx): void {
  const base = '#4f6f8f';
  const hi = '#6a8cae';
  const dark = '#37506b';
  r(c, 0, 0, 48, 26, dark);
  r(c, 1, 1, 46, 11, base);
  r(c, 1, 1, 46, 1, hi);
  r(c, 0, 8, 5, 18, base);
  r(c, 43, 8, 5, 18, base);
  r(c, 0, 8, 5, 1, hi);
  r(c, 43, 8, 5, 1, hi);
  for (let i = 0; i < 3; i++) {
    const x = 5 + i * 13;
    r(c, x, 11, 12, 10, hi);
    r(c, x, 11, 12, 1, '#8cabc9');
    r(c, x + 11, 12, 1, 9, base);
  }
  r(c, 0, 22, 48, 4, dark);
  r(c, 3, 25, 3, 1, '#1d2a3a');
  r(c, 42, 25, 3, 1, '#1d2a3a');
  // cushion pillow
  r(c, 7, 6, 5, 5, '#e0a63a');
  r(c, 7, 6, 5, 1, '#f5c866');
}

function drawCoffee(c: Ctx): void {
  r(c, 2, 4, 12, 22, '#3a3f4d');
  r(c, 2, 4, 12, 2, '#5a6275');
  r(c, 3, 7, 10, 5, '#15181f');
  r(c, 4, 8, 4, 1, PAL.amber);
  r(c, 4, 10, 6, 1, '#6b7a8f');
  r(c, 11, 8, 2, 2, PAL.red);
  r(c, 5, 15, 6, 6, '#1f232d');
  r(c, 6, 18, 4, 3, '#e9eef5');
  r(c, 4, 22, 8, 4, '#2b2f3a');
  r(c, 2, 24, 12, 2, '#262a35');
  // pot on top
  r(c, 5, 0, 6, 4, '#8fc7e8');
  r(c, 5, 2, 6, 2, '#6b3f26');
}

function drawCooler(c: Ctx): void {
  r(c, 3, 0, 10, 12, '#8fc7e8');
  r(c, 3, 0, 10, 2, '#d8f0ff');
  r(c, 4, 3, 2, 8, '#bfe6fa');
  r(c, 3, 11, 10, 1, '#4b83a8');
  r(c, 2, 12, 12, 16, '#e6ebf2');
  r(c, 2, 12, 12, 1, '#ffffff');
  r(c, 2, 26, 12, 2, '#9aa7b8');
  r(c, 5, 16, 3, 2, '#4a8fe0');
  r(c, 9, 16, 3, 2, '#e0524f');
  r(c, 5, 21, 6, 3, '#9aa7b8');
}
