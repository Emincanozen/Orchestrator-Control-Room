/** Bakes floors, walls, rugs, wall decor and name plates into ONE canvas (drawn once). */
import { LAYOUT, type FloorStyle, type Layout } from '../layout';
import { TILE } from '../types';
import { PAL, numToHex, shade } from './color';
import { drawPixelText, textWidth } from './pixelFont';

type Ctx = CanvasRenderingContext2D;
const r = (c: Ctx, x: number, y: number, w: number, h: number, col: string) => {
  c.fillStyle = col;
  c.fillRect(x, y, w, h);
};

const hash2 = (x: number, y: number, s = 0) => {
  let h = (x * 374761393 + y * 668265263 + s * 2147483647) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
};

const FLOOR: Record<FloorStyle, { a: string; b: string; seam: string }> = {
  'wood-dark': { a: '#4d3829', b: '#45322a', seam: '#33241b' },
  'tile-blue': { a: '#3a4a63', b: '#344359', seam: '#27324a' },
  'carpet-teal': { a: '#2d5a5c', b: '#2a5254', seam: '#244648' },
  'wood-green': { a: '#506042', b: '#485839', seam: '#35432b' },
  concrete: { a: '#5a6068', b: '#535960', seam: '#42484f' },
  'carpet-violet': { a: '#4a3f66', b: '#443a5f', seam: '#382f50' },
  'wood-warm': { a: '#7a5638', b: '#725032', seam: '#553a25' },
  'tile-cream': { a: '#b9b09a', b: '#aea58f', seam: '#8c846f' },
  corridor: { a: '#303a4b', b: '#2c3545', seam: '#232b39' },
};

function floorTile(c: Ctx, style: FloorStyle, tx: number, ty: number): void {
  const x = tx * TILE;
  const y = ty * TILE;
  const f = FLOOR[style];
  const alt = (tx + ty) % 2 === 0;
  r(c, x, y, TILE, TILE, alt ? f.a : f.b);
  if (style.startsWith('wood')) {
    for (let i = 0; i < 4; i++) r(c, x, y + i * 4 + 3, TILE, 1, f.seam);
    // staggered plank joints
    for (let i = 0; i < 4; i++) {
      const jx = Math.floor(hash2(tx, ty * 4 + i, 3) * 14) + 1;
      r(c, x + jx, y + i * 4, 1, 3, f.seam);
    }
    r(c, x, y + 4, TILE, 1, shade(f.a, 1.07));
  } else if (style.startsWith('tile')) {
    r(c, x, y + 7, TILE, 1, f.seam);
    r(c, x + 7, y, 1, TILE, f.seam);
    r(c, x + 15, y, 1, TILE, f.seam);
    r(c, x, y + 15, TILE, 1, f.seam);
    r(c, x + 1, y + 1, 6, 1, shade(f.a, 1.1));
    r(c, x + 9, y + 9, 6, 1, shade(f.a, 1.1));
  } else if (style.startsWith('carpet')) {
    for (let i = 0; i < 10; i++) {
      const px = Math.floor(hash2(tx, ty, i) * 16);
      const py = Math.floor(hash2(tx, ty, i + 40) * 16);
      r(c, x + px, y + py, 1, 1, i % 2 ? shade(f.a, 1.12) : f.seam);
    }
  } else if (style === 'concrete') {
    for (let i = 0; i < 8; i++) {
      const px = Math.floor(hash2(tx, ty, i) * 16);
      const py = Math.floor(hash2(tx, ty, i + 40) * 16);
      r(c, x + px, y + py, 1, 1, i % 2 ? shade(f.a, 1.14) : shade(f.a, 0.86));
    }
    r(c, x, y + 15, TILE, 1, f.seam);
    r(c, x + 15, y, 1, TILE, f.seam);
  } else {
    // corridor: subtle runner lines
    r(c, x, y + 15, TILE, 1, f.seam);
    if ((tx + ty) % 4 === 0) r(c, x + 6, y + 6, 4, 1, shade(f.a, 1.15));
    if (ty === 10) r(c, x, y, TILE, 1, shade(f.a, 1.1));
  }
}

function wallCap(c: Ctx, x: number, y: number): void {
  r(c, x, y, TILE, TILE, PAL.wallCap);
  r(c, x, y, TILE, 2, PAL.wallCapLight);
  r(c, x, y + 14, TILE, 2, '#232b39');
  r(c, x + 7, y + 2, 2, 12, '#262f3e');
}

function wallFace(c: Ctx, x: number, y: number, tx: number): void {
  r(c, x, y, TILE, TILE, PAL.wallFace);
  r(c, x, y, TILE, 3, PAL.wallCapLight);
  r(c, x, y + 3, TILE, 1, '#232b39');
  // panel seams and skirting
  r(c, x + 15, y + 4, 1, 10, PAL.wallFaceDark);
  r(c, x, y + 4, TILE, 1, '#566882');
  r(c, x, y + 14, TILE, 2, PAL.wallBase);
  r(c, x, y + 13, TILE, 1, '#33404f');
  if (tx % 2 === 0) r(c, x + 3, y + 8, 2, 1, '#52627b');
}

function rug(c: Ctx, rg: Layout['rugs'][number]): void {
  const pal = {
    blue: ['#2f4f86', '#3f64a3', '#8fb2e8'],
    red: ['#7a2f3a', '#963d49', '#e0a0a8'],
    green: ['#2f6a45', '#3f8558', '#a8e0b8'],
    violet: ['#5a3f86', '#7050a3', '#c8b0f0'],
    sand: ['#a8895a', '#bf9f6c', '#f0dcb0'],
  }[rg.style];
  const x = rg.x * TILE + 2;
  const y = rg.y * TILE + 2;
  const w = rg.w * TILE - 4;
  const h = rg.h * TILE - 4;
  r(c, x, y, w, h, pal[0]);
  r(c, x + 2, y + 2, w - 4, h - 4, pal[1]);
  r(c, x + 3, y + 3, w - 6, 1, pal[2]);
  r(c, x + 3, y + h - 4, w - 6, 1, pal[2]);
  for (let i = 0; i < w; i += 3) {
    r(c, x + i, y - 1, 1, 1, pal[2]);
    r(c, x + i, y + h, 1, 1, pal[2]);
  }
  // diamond motif
  const cx = x + Math.floor(w / 2);
  const cy = y + Math.floor(h / 2);
  r(c, cx - 1, cy - 3, 2, 6, pal[2]);
  r(c, cx - 3, cy - 1, 6, 2, pal[2]);
  r(c, cx - 1, cy - 1, 2, 2, pal[0]);
}

function windowDecor(c: Ctx, x: number, y: number): void {
  r(c, x + 1, y + 4, 14, 11, '#232b39');
  r(c, x + 2, y + 5, 12, 8, PAL.sky);
  r(c, x + 2, y + 5, 12, 3, '#c6ecfb');
  r(c, x + 7, y + 5, 2, 8, '#e8f0fa');
  r(c, x + 2, y + 8, 12, 1, '#e8f0fa');
  r(c, x + 3, y + 6, 3, 1, '#ffffff');
  r(c, x + 1, y + 13, 14, 2, '#9aa7b8');
  // far skyline
  r(c, x + 3, y + 11, 2, 2, '#7aa6c4');
  r(c, x + 10, y + 10, 2, 3, '#7aa6c4');
}

function clockDecor(c: Ctx, x: number, y: number): void {
  r(c, x + 4, y + 4, 8, 9, '#2a303c');
  r(c, x + 3, y + 5, 10, 7, '#2a303c');
  r(c, x + 5, y + 5, 6, 7, '#e9eef5');
  r(c, x + 4, y + 6, 8, 5, '#e9eef5');
  r(c, x + 8, y + 6, 1, 3, '#14161f');
  r(c, x + 8, y + 8, 2, 1, '#14161f');
  r(c, x + 8, y + 5, 1, 1, PAL.red);
}

function posterDecor(c: Ctx, x: number, y: number, seed: number): void {
  r(c, x + 3, y + 4, 10, 10, '#232b39');
  const pals = [
    ['#2f4f86', '#38e8c6', '#f5b94a'],
    ['#7a2f3a', '#f5b94a', '#e9eef5'],
    ['#3f2f66', '#f472b6', '#8fc7e8'],
  ][seed % 3];
  r(c, x + 4, y + 5, 8, 8, pals[0]);
  r(c, x + 5, y + 6, 6, 2, pals[1]);
  r(c, x + 5, y + 9, 3, 3, pals[2]);
  r(c, x + 9, y + 9, 2, 3, pals[1]);
}

function boardDecor(c: Ctx, x: number, y: number): void {
  r(c, x + 1, y + 4, 14, 10, '#8b98a9');
  r(c, x + 2, y + 5, 12, 8, '#eef3f8');
  r(c, x + 3, y + 6, 6, 1, '#4f8fe0');
  r(c, x + 3, y + 8, 8, 1, '#e0524f');
  r(c, x + 3, y + 10, 4, 1, '#4ac07a');
  r(c, x + 9, y + 10, 3, 2, '#f5b94a');
  r(c, x + 2, y + 13, 12, 1, '#6c7e99');
}

export function bakeWorld(c: Ctx, layout: Layout = LAYOUT): void {
  const { cols, rows, tiles } = layout;
  r(c, 0, 0, cols * TILE, rows * TILE, PAL.void);
  for (let ty = 0; ty < rows; ty++) {
    for (let tx = 0; tx < cols; tx++) {
      const t = tiles[ty][tx];
      const x = tx * TILE;
      const y = ty * TILE;
      if (t.kind === 'wall') {
        if (t.face) wallFace(c, x, y, tx);
        else wallCap(c, x, y);
        continue;
      }
      const room = t.room === 'corridor' || t.room === null ? null : layout.roomById[t.room];
      floorTile(c, room ? room.floor : 'corridor', tx, ty);
      if (t.kind === 'door') {
        // door opening: threshold and frame jambs
        r(c, x, y, 2, TILE, '#5f3e27');
        r(c, x + 14, y, 2, TILE, '#5f3e27');
        r(c, x + 2, y + 6, 12, 4, '#7d5636');
        r(c, x + 2, y + 6, 12, 1, '#a8754b');
        r(c, x + 1, y, 1, TILE, '#8a5d3b');
        r(c, x + 14, y, 1, TILE, '#8a5d3b');
      }
    }
  }
  // soft shading where floor meets walls
  c.fillStyle = 'rgba(0,0,0,0.22)';
  for (let ty = 1; ty < rows; ty++) {
    for (let tx = 0; tx < cols; tx++) {
      if (tiles[ty][tx].kind === 'wall') continue;
      if (tiles[ty - 1][tx].kind === 'wall') c.fillRect(tx * TILE, ty * TILE, TILE, 3);
      if (tx > 0 && tiles[ty][tx - 1].kind === 'wall') c.fillRect(tx * TILE, ty * TILE, 2, TILE);
    }
  }
  for (const rg of layout.rugs) rug(c, rg);

  for (const d of layout.decor) {
    const x = d.x * TILE;
    const y = d.y * TILE;
    switch (d.kind) {
      case 'window':
        windowDecor(c, x, y);
        break;
      case 'clock':
        clockDecor(c, x, y);
        break;
      case 'poster':
        posterDecor(c, x, y, d.x);
        break;
      case 'board':
        boardDecor(c, x, y);
        break;
      case 'plate': {
        const room = layout.roomById[d.room as keyof typeof layout.roomById];
        const label = d.label ?? '';
        const w = textWidth(label) + 6;
        const px = Math.round(x + (5 * TILE) / 2 - w / 2);
        const py = y + 5;
        const acc = numToHex(room.accent);
        r(c, px, py, w, 9, '#1a202b');
        r(c, px, py, w, 1, acc);
        r(c, px, py + 8, w, 1, shade(acc, 0.6));
        drawPixelText(c, label, px + 3, py + 2, '#e9eef5');
        r(c, px - 2, py + 3, 2, 2, acc);
        r(c, px + w, py + 3, 2, 2, acc);
        break;
      }
    }
  }

  // contact shadows under blocking furniture
  c.fillStyle = 'rgba(0,0,0,0.28)';
  for (const f of layout.furniture) {
    if (!f.blocks) continue;
    c.fillRect(f.x * TILE + 1, (f.y + f.h) * TILE, f.w * TILE - 1, 2);
  }
}
