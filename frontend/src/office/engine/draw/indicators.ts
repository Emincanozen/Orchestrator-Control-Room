/** Overlay indicator, ring and shadow sprites, drawn procedurally. */
import type { Indicator } from '../types';

type Ctx = CanvasRenderingContext2D;
const r = (c: Ctx, x: number, y: number, w: number, h: number, col: string) => {
  c.fillStyle = col;
  c.fillRect(x, y, w, h);
};

export interface IconSpec {
  w: number;
  h: number;
  draw: (c: Ctx, frame: number) => void;
}

const INK = '#1b1c28';

/** Adds a 1px dark outline around everything drawn inside [pad, size-pad). */
function outline(c: Ctx, w: number, h: number): void {
  const img = c.getImageData(0, 0, w, h);
  const s = img.data;
  const out = new Uint8ClampedArray(s);
  const solid = (x: number, y: number) => x >= 0 && y >= 0 && x < w && y < h && s[(y * w + x) * 4 + 3] > 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (s[i + 3] > 0) continue;
      if (solid(x - 1, y) || solid(x + 1, y) || solid(x, y - 1) || solid(x, y + 1)) {
        out[i] = 0x1b;
        out[i + 1] = 0x1c;
        out[i + 2] = 0x28;
        out[i + 3] = 255;
      }
    }
  }
  c.putImageData(new ImageData(out, w, h), 0, 0);
}

export const ICON_SIZE: Record<Indicator, { w: number; h: number; frames: number }> = {
  warning: { w: 15, h: 14, frames: 1 },
  approval: { w: 15, h: 16, frames: 1 },
  blocked: { w: 14, h: 14, frames: 1 },
  sleep: { w: 14, h: 14, frames: 2 },
  speech: { w: 18, h: 14, frames: 3 },
};

export function drawIndicator(c: Ctx, kind: Indicator, frame: number): void {
  const { w, h } = ICON_SIZE[kind];
  switch (kind) {
    case 'warning': {
      for (let i = 0; i < 10; i++) {
        const half = Math.floor(i / 1.6);
        r(c, 7 - half, 2 + i, 1 + half * 2, 1, i < 1 ? '#ffd98a' : '#fb923c');
      }
      r(c, 3, 11, 9, 1, '#fb923c');
      r(c, 7, 5, 1, 4, INK);
      r(c, 7, 10, 1, 1, INK);
      break;
    }
    case 'approval': {
      r(c, 2, 2, 11, 10, '#facc15');
      r(c, 1, 3, 13, 8, '#facc15');
      r(c, 2, 2, 11, 1, '#fff2a8');
      r(c, 2, 11, 11, 1, '#c9a30c');
      r(c, 5, 12, 3, 1, '#facc15');
      r(c, 6, 13, 1, 1, '#facc15');
      r(c, 7, 4, 2, 4, INK);
      r(c, 7, 9, 2, 2, INK);
      break;
    }
    case 'blocked': {
      r(c, 4, 1, 6, 12, '#ef4444');
      r(c, 1, 4, 12, 6, '#ef4444');
      r(c, 2, 2, 10, 10, '#ef4444');
      r(c, 2, 2, 10, 1, '#ff9a9a');
      for (let i = 0; i < 6; i++) {
        r(c, 4 + i, 4 + i, 1, 1, '#ffffff');
        r(c, 9 - i, 4 + i, 1, 1, '#ffffff');
      }
      break;
    }
    case 'sleep': {
      const o = frame % 2;
      r(c, 2, 7 - o, 4, 1, '#cfe3ff');
      r(c, 4, 8 - o, 1, 1, '#cfe3ff');
      r(c, 3, 9 - o, 1, 1, '#cfe3ff');
      r(c, 2, 10 - o, 4, 1, '#cfe3ff');
      r(c, 8, 2 + o, 5, 1, '#cfe3ff');
      r(c, 11, 3 + o, 1, 1, '#cfe3ff');
      r(c, 10, 4 + o, 1, 1, '#cfe3ff');
      r(c, 9, 5 + o, 1, 1, '#cfe3ff');
      r(c, 8, 6 + o, 5, 1, '#cfe3ff');
      break;
    }
    case 'speech': {
      r(c, 2, 2, 14, 8, '#f4f7fb');
      r(c, 1, 3, 16, 6, '#f4f7fb');
      r(c, 4, 10, 3, 1, '#f4f7fb');
      r(c, 4, 11, 1, 1, '#f4f7fb');
      const dots = (frame % 3) + 1;
      for (let i = 0; i < 3; i++) r(c, 5 + i * 3, 5, 2, 2, i < dots ? '#3a475c' : '#c2cbd8');
      break;
    }
  }
  outline(c, w, h);
}

export function drawShadow(c: Ctx): void {
  c.fillStyle = 'rgba(8,10,16,0.38)';
  c.fillRect(2, 1, 8, 2);
  c.fillRect(1, 1, 10, 1);
  c.fillRect(3, 0, 6, 1);
  c.fillRect(3, 3, 6, 1);
}

/** Elliptical ring, 24x10. */
export function drawRing(c: Ctx, color: string): void {
  c.fillStyle = color;
  const cx = 11.5;
  const cy = 4.5;
  for (let y = 0; y < 10; y++) {
    for (let x = 0; x < 24; x++) {
      const d = Math.pow((x - cx) / 11.5, 2) + Math.pow((y - cy) / 4.5, 2);
      if (d <= 1.02 && d >= 0.52) c.fillRect(x, y, 1, 1);
    }
  }
}

/** Selection chevron pointing down, 11x7 (outlined). */
export function drawChevron(c: Ctx, color: string): void {
  for (let i = 0; i < 4; i++) r(c, 1 + i, 1 + i, 9 - 2 * i, 1, color);
  r(c, 4, 5, 3, 1, color);
  outline(c, 11, 7);
}
