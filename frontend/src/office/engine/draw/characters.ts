/**
 * Procedural character art. Every frame is drawn with fillRect on a 16x24 cell.
 * A whole atlas (all poses/directions/animations/frames) is generated once per
 * appearance and reused: nothing is redrawn per frame at runtime.
 */
import type { Appearance } from '../appearance';
import { ANIM_FRAMES, frameKey, type AnimName, type Dir, type FrameSpec, type Pose } from '../types';
import { shade } from './color';

export const CELL_W = 16;
export const CELL_H = 24;
/** feet line: bottom edge of the shoe row (anchor y = FEET_Y / CELL_H) */
export const FEET_Y = 23;
const B = 1; // vertical base offset (room for the outline)
const INK = '#1b1c28';

const DIRS: Dir[] = ['down', 'up', 'left', 'right'];
const ANIMS = Object.keys(ANIM_FRAMES) as AnimName[];
const POSES: Pose[] = ['stand', 'sit'];

export function enumerateFrames(): FrameSpec[] {
  const out: FrameSpec[] = [];
  for (const pose of POSES) for (const dir of DIRS) for (const anim of ANIMS) for (let f = 0; f < ANIM_FRAMES[anim]; f++) out.push({ pose, dir, anim, f });
  return out;
}

type Ctx = CanvasRenderingContext2D;
const r = (c: Ctx, x: number, y: number, w: number, h: number, col: string) => {
  c.fillStyle = col;
  c.fillRect(x, y, w, h);
};

interface Cols {
  skin: string;
  skinSh: string;
  hair: string;
  hairSh: string;
  outfit: string;
  outfitSh: string;
  outfitHi: string;
  pants: string;
  pantsSh: string;
  shoe: string;
}

function colsOf(ap: Appearance): Cols {
  return {
    skin: ap.skin,
    skinSh: shade(ap.skin, 0.82),
    hair: ap.hair,
    hairSh: shade(ap.hair, 0.7),
    outfit: ap.outfit,
    outfitSh: shade(ap.outfit, 0.74),
    outfitHi: shade(ap.outfit, 1.18),
    pants: ap.pants,
    pantsSh: shade(ap.pants, 0.75),
    shoe: '#2a2630',
  };
}

export function drawFrame(c: Ctx, ap: Appearance, spec: FrameSpec): void {
  if (spec.dir === 'right') {
    c.save();
    c.translate(CELL_W, 0);
    c.scale(-1, 1);
    drawSideOrFB(c, ap, { ...spec, dir: 'left' });
    c.restore();
  } else drawSideOrFB(c, ap, spec);
}

function drawSideOrFB(c: Ctx, ap: Appearance, spec: FrameSpec): void {
  const k = colsOf(ap);
  const { pose, dir, anim, f } = spec;
  const sit = pose === 'sit';
  const side = dir === 'left';
  const back = dir === 'up';
  let by = sit ? B + 4 : B;
  if (anim === 'walk' && f % 2 === 1) by -= 1;
  const breath = (anim === 'idle' && f === 1) || anim === 'sleep';
  let hy = by + (breath && anim === 'idle' ? 1 : 0);
  if (anim === 'sleep') hy = by + 2 + f;
  const blink = anim === 'idle' && f === 2;
  const mouthOpen = anim === 'talk' ? f === 1 : anim === 'celebrate';
  const eyesShut = blink || anim === 'sleep';

  // ---- legs (standing only)
  if (!sit) {
    const lift = (leg: 0 | 1) => anim === 'walk' && ((f === 1 && leg === 0) || (f === 3 && leg === 1));
    if (!side) {
      for (const leg of [0, 1] as const) {
        const x = leg === 0 ? 5 : 8;
        const up = lift(leg) ? 1 : 0;
        r(c, x, B + 16, 3, 5 - up, leg === 0 ? k.pants : k.pantsSh);
        r(c, x, B + 20 - up + 0, 3, 2, k.shoe);
        if (up) r(c, x, B + 22 - up, 3, 0, k.shoe);
      }
    } else {
      const spread = anim === 'walk' && (f === 1 || f === 3);
      const a = spread ? (f === 1 ? 4 : 9) : 6;
      const b = spread ? (f === 1 ? 9 : 4) : 7;
      r(c, b, B + 16, 3, 5, k.pantsSh);
      r(c, b, B + 20, 3, 2, shade(k.shoe, 0.8));
      r(c, a, B + 16, 3, 5, k.pants);
      r(c, a - (a < 6 ? 1 : 0), B + 20, 4, 2, k.shoe);
    }
  }

  // ---- torso
  if (!side) {
    r(c, 4, by + 10, 8, 7, k.outfit);
    r(c, 4, by + 16, 8, 1, k.outfitSh);
    r(c, 4, by + 10, 8, 1, k.outfitHi);
    r(c, 11, by + 11, 1, 5, k.outfitSh);
  } else {
    r(c, 5, by + 10, 6, 7, k.outfit);
    r(c, 5, by + 16, 6, 1, k.outfitSh);
    r(c, 5, by + 10, 6, 1, k.outfitHi);
  }
  const acc = ap.accessory;
  if (acc === 'crown-tie') {
    if (!side) {
      if (!back) {
        r(c, 7, by + 10, 2, 1, '#e8e8ee');
        r(c, 7, by + 11, 2, 5, '#c43b3b');
        r(c, 7, by + 11, 2, 1, '#8a2424');
      }
      r(c, 4, by + 10, 1, 2, '#f0c24a');
      r(c, 11, by + 10, 1, 2, '#f0c24a');
    } else {
      r(c, 5, by + 10, 1, 6, '#c43b3b');
      r(c, 10, by + 10, 1, 2, '#f0c24a');
    }
  }
  if (acc === 'glasses-scarf') {
    if (!side) {
      r(c, 4, by + 10, 8, 2, '#dcc2ff');
      if (!back) r(c, 9, by + 12, 2, 4, '#dcc2ff');
      else r(c, 4, by + 12, 2, 3, '#dcc2ff');
    } else r(c, 5, by + 10, 6, 2, '#dcc2ff');
  }

  // ---- arms & props
  drawArms(c, k, spec, by, side, back);

  // ---- head
  r(c, 4, hy + 2, 8, 8, k.skin);
  if (!side) r(c, 11, hy + 3, 1, 7, k.skinSh);
  drawHair(c, k, ap, hy, side, back);
  if (!back) drawFace(c, k, ap, hy, side, eyesShut, mouthOpen);
  drawHeadAcc(c, ap, hy, side, back);
}

function drawArms(c: Ctx, k: Cols, spec: FrameSpec, by: number, side: boolean, back: boolean): void {
  const { anim, f, pose } = spec;
  const sit = pose === 'sit';
  const sl = k.outfitSh;
  const L = (dy: number, h: number) => r(c, 2, by + 10 + dy, 2, h, sl);
  const R = (dy: number, h: number) => r(c, 12, by + 10 + dy, 2, h, sl);
  const handL = (dy: number) => r(c, 2, by + 10 + dy, 2, 2, k.skin);
  const handR = (dy: number) => r(c, 12, by + 10 + dy, 2, 2, k.skin);
  const swing = anim === 'walk' ? (f === 1 ? [1, -1] : f === 3 ? [-1, 1] : [0, 0]) : [0, 0];

  if (side) {
    // single visible arm in front of the torso
    const armX = 6;
    switch (anim) {
      case 'type':
      case 'test':
        r(c, armX - 3, by + 11 + (f % 2), 5, 2, sl);
        r(c, armX - 4, by + 11 + (f % 2), 2, 2, k.skin);
        break;
      case 'read':
        r(c, armX - 2, by + 10, 4, 2, sl);
        r(c, armX - 4, by + 8, 5, 5, '#e9eef5');
        r(c, armX - 3, by + 9, 3, 1, '#9aa7b8');
        r(c, armX - 3, by + 11, 3, 1, '#9aa7b8');
        break;
      case 'review':
        r(c, armX - 2, by + 10, 4, 2, sl);
        r(c, armX - 5, by + 8 - (f % 2), 4, 4, '#8fc7e8');
        r(c, armX - 5, by + 8 - (f % 2), 4, 1, '#3a475c');
        break;
      case 'talk':
        r(c, armX - 1, by + 8 + (f % 2) * 2, 2, 4, sl);
        r(c, armX - 1, by + 7 + (f % 2) * 2, 2, 2, k.skin);
        break;
      case 'celebrate':
        r(c, armX - 1, by + 4, 2, 6, sl);
        r(c, armX - 1, by + 3, 2, 2, k.skin);
        break;
      default:
        r(c, armX, by + 10 + (swing[0] ?? 0), 3, 5, sl);
        r(c, armX, by + 15 + (swing[0] ?? 0), 2, 2, k.skin);
    }
    return;
  }

  switch (anim) {
    case 'type':
    case 'test': {
      // elbows out, hands forward over the keyboard (hidden by the torso from behind)
      const a = f % 2;
      L(1 + a, 4);
      R(2 - a, 4);
      if (!back) {
        handL(5 + a);
        handR(6 - a);
      } else {
        r(c, 2, by + 14 + a, 2, 1, k.skin);
        r(c, 12, by + 15 - a, 2, 1, k.skin);
      }
      break;
    }
    case 'read': {
      L(0, 5);
      handL(5);
      // right hand holds a page up beside the head
      R(-2, 5);
      const up = f % 2;
      r(c, 11, by + 3 - up, 4, 6, '#e9eef5');
      r(c, 12, by + 4 - up, 2, 1, '#9aa7b8');
      r(c, 12, by + 6 - up, 2, 1, '#9aa7b8');
      r(c, 12, by + 8 - up + 0, 1, 0, '#9aa7b8');
      handR(2 - up);
      break;
    }
    case 'review': {
      // clipboard in the left hand, magnifier in the right
      L(0, 5);
      handL(4);
      r(c, 0, by + 4, 4, 7, '#a8754b');
      r(c, 1, by + 5, 2, 5, '#e9eef5');
      r(c, 1, by + 3, 2, 1, '#8b98a9');
      R(-2, 5);
      const m = f % 2;
      r(c, 12, by + 1 - m, 3, 3, '#8fc7e8');
      r(c, 12, by + 1 - m, 3, 1, '#e8f6ff');
      r(c, 12, by + 4 - m, 1, 3, '#6b4a2e');
      handR(2 - m);
      break;
    }
    case 'talk': {
      L(0, 5);
      handL(5);
      const g = f % 2;
      R(-1 - g * 2, 5);
      handR(-2 - g * 2);
      break;
    }
    case 'celebrate': {
      const w = f % 2;
      L(-6 + w, 7);
      R(-6 + (1 - w), 7);
      handL(-8 + w);
      handR(-8 + (1 - w));
      break;
    }
    case 'sleep':
      L(2, 4);
      R(2, 4);
      break;
    default: {
      L(swing[0] ?? 0, 5);
      R(swing[1] ?? 0, 5);
      if (!sit) {
        handL(5 + (swing[0] ?? 0));
        handR(5 + (swing[1] ?? 0));
      } else if (!back) {
        handL(4);
        handR(4);
      }
    }
  }
}

function drawHair(c: Ctx, k: Cols, ap: Appearance, hy: number, side: boolean, back: boolean): void {
  const h = k.hair;
  const style = ap.hairStyle;
  if (back) {
    r(c, 4, hy + 1, 8, style === 'long' ? 10 : 8, h);
    r(c, 4, hy + 8 + (style === 'long' ? 2 : 0), 8, 1, k.hairSh);
    if (style === 'spiky') {
      r(c, 4, hy + 0, 2, 1, h);
      r(c, 7, hy + 0, 2, 1, h);
      r(c, 10, hy + 0, 2, 1, h);
    }
    if (style === 'cap') {
      r(c, 3, hy + 1, 10, 3, shade(ap.outfit, 0.9));
      r(c, 3, hy + 3, 10, 1, shade(ap.outfit, 0.7));
    }
    return;
  }
  if (side) {
    r(c, 4, hy + 1, 8, 3, h);
    r(c, 7, hy + 4, 5, 5, h);
    r(c, 7, hy + 8, 5, 1, k.hairSh);
    if (style === 'long') r(c, 8, hy + 4, 4, 8, h);
    if (style === 'spiky') {
      r(c, 4, hy, 2, 1, h);
      r(c, 7, hy, 2, 1, h);
      r(c, 10, hy, 2, 1, h);
    }
    if (style === 'cap') {
      r(c, 4, hy + 1, 8, 3, shade(ap.outfit, 0.9));
      r(c, 2, hy + 3, 5, 1, shade(ap.outfit, 0.7));
    }
    return;
  }
  // front
  r(c, 4, hy + 1, 8, 3, h);
  r(c, 4, hy + 4, 1, 2, h);
  r(c, 11, hy + 4, 1, 2, k.hairSh);
  r(c, 4, hy + 3, 8, 1, k.hairSh);
  if (style === 'long') {
    r(c, 3, hy + 2, 2, 9, h);
    r(c, 11, hy + 2, 2, 9, k.hairSh);
  }
  if (style === 'spiky') {
    r(c, 4, hy, 2, 1, h);
    r(c, 7, hy, 2, 1, h);
    r(c, 10, hy, 2, 1, h);
  }
  if (style === 'cap') {
    r(c, 4, hy + 1, 8, 3, shade(ap.outfit, 0.9));
    r(c, 3, hy + 3, 10, 1, shade(ap.outfit, 0.7));
  }
}

function drawFace(c: Ctx, k: Cols, ap: Appearance, hy: number, side: boolean, shut: boolean, open: boolean): void {
  const eye = '#1d1d28';
  const mouth = open ? '#5a1f24' : '#a8504a';
  if (!side) {
    if (shut) {
      r(c, 6, hy + 6, 1, 1, eye);
      r(c, 9, hy + 6, 1, 1, eye);
    } else {
      r(c, 6, hy + 5, 1, 2, eye);
      r(c, 9, hy + 5, 1, 2, eye);
    }
    r(c, 7, hy + 8, 2, open ? 2 : 1, mouth);
    if (ap.accessory === 'glasses' || ap.accessory === 'glasses-scarf') {
      const g = '#2b2f40';
      r(c, 5, hy + 4, 3, 1, g);
      r(c, 8, hy + 4, 3, 1, g);
      r(c, 5, hy + 7, 3, 1, g);
      r(c, 8, hy + 7, 3, 1, g);
      r(c, 5, hy + 5, 1, 2, g);
      r(c, 7, hy + 5, 1, 2, g);
      r(c, 8, hy + 5, 1, 2, g);
      r(c, 10, hy + 5, 1, 2, g);
    }
  } else {
    if (shut) r(c, 5, hy + 6, 1, 1, eye);
    else r(c, 5, hy + 5, 1, 2, eye);
    r(c, 3, hy + 6, 1, 1, k.skin);
    r(c, 4, hy + 8, 2, open ? 2 : 1, mouth);
    if (ap.accessory === 'glasses' || ap.accessory === 'glasses-scarf') {
      const g = '#2b2f40';
      r(c, 4, hy + 4, 3, 1, g);
      r(c, 4, hy + 7, 3, 1, g);
      r(c, 4, hy + 5, 1, 2, g);
      r(c, 6, hy + 5, 1, 2, g);
      r(c, 7, hy + 5, 4, 1, g);
    }
  }
}

function drawHeadAcc(c: Ctx, ap: Appearance, hy: number, side: boolean, back: boolean): void {
  switch (ap.accessory) {
    case 'headphones':
      r(c, 4, hy + 1, 8, 1, '#2c3342');
      if (!side) {
        r(c, 3, hy + 4, 2, 4, '#2c3342');
        r(c, 11, hy + 4, 2, 4, '#2c3342');
        r(c, 3, hy + 5, 1, 2, '#38e8c6');
        r(c, 12, hy + 5, 1, 2, '#38e8c6');
      } else {
        r(c, 6, hy + 4, 3, 4, '#2c3342');
        r(c, 7, hy + 5, 1, 2, '#38e8c6');
      }
      break;
    case 'beret':
      r(c, side ? 3 : 3, hy + 1, 9, 2, '#c0463f');
      r(c, 4, hy, 7, 1, '#c0463f');
      r(c, 8, hy - 1, 2, 1, '#8f2f2a');
      break;
    case 'headband':
      r(c, 4, hy + 3, 8, 1, '#f0a640');
      if (back) r(c, 4, hy + 3, 8, 1, '#f0a640');
      break;
    case 'crown-tie':
      r(c, 5, hy + 1, 6, 1, '#f0c24a');
      r(c, 5, hy, 1, 1, '#f0c24a');
      r(c, 7, hy, 2, 1, '#f0c24a');
      r(c, 10, hy, 1, 1, '#f0c24a');
      r(c, 7, hy + 1, 2, 1, '#e05a5a');
      break;
    default:
  }
}

export interface AtlasInfo {
  canvas: HTMLCanvasElement | OffscreenCanvas;
  cols: number;
  index: Map<string, number>;
}

const COLS = 16;

/** Draws every frame of one appearance onto a single atlas canvas, then adds the dark outline. */
export function buildCharacterAtlas(ap: Appearance, make: (w: number, h: number) => HTMLCanvasElement): AtlasInfo {
  const frames = enumerateFrames();
  const rows = Math.ceil(frames.length / COLS);
  const canvas = make(COLS * CELL_W, rows * CELL_H);
  const ctx = canvas.getContext('2d', { willReadFrequently: true }) as CanvasRenderingContext2D;
  const index = new Map<string, number>();
  frames.forEach((spec, i) => {
    index.set(frameKey(spec), i);
    ctx.save();
    ctx.translate((i % COLS) * CELL_W, Math.floor(i / COLS) * CELL_H);
    ctx.beginPath();
    ctx.rect(0, 0, CELL_W, CELL_H);
    ctx.clip();
    drawFrame(ctx, ap, spec);
    ctx.restore();
  });
  outlineAtlas(ctx, canvas.width, canvas.height);
  return { canvas, cols: COLS, index };
}

function outlineAtlas(ctx: Ctx, w: number, h: number): void {
  const img = ctx.getImageData(0, 0, w, h);
  const src = img.data;
  const out = new Uint8ClampedArray(src);
  const solid = (x: number, y: number) => x >= 0 && y >= 0 && x < w && y < h && src[(y * w + x) * 4 + 3] > 0;
  const ink = [0x1b, 0x1c, 0x28];
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (src[i + 3] > 0) continue;
      // do not bleed across cell borders
      const cx = x % CELL_W;
      const cy = y % CELL_H;
      const n =
        (cx > 0 && solid(x - 1, y)) || (cx < CELL_W - 1 && solid(x + 1, y)) || (cy > 0 && solid(x, y - 1)) || (cy < CELL_H - 1 && solid(x, y + 1));
      if (n) {
        out[i] = ink[0];
        out[i + 1] = ink[1];
        out[i + 2] = ink[2];
        out[i + 3] = 255;
      }
    }
  }
  ctx.putImageData(new ImageData(out, w, h), 0, 0);
  void INK;
}
