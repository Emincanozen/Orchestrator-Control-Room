/** Tiny colour helpers and the office palette (one limited palette for all art). */

export function shade(hex: string, f: number): string {
  const n = parseInt(hex.slice(1), 16);
  const r = Math.max(0, Math.min(255, Math.round(((n >> 16) & 255) * f)));
  const g = Math.max(0, Math.min(255, Math.round(((n >> 8) & 255) * f)));
  const b = Math.max(0, Math.min(255, Math.round((n & 255) * f)));
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}

export const numToHex = (n: number): string => `#${n.toString(16).padStart(6, '0')}`;

/** Palette harmonised with the dashboard tokens (slate surfaces, phosphor accents). */
export const PAL = {
  outline: '#14161f',
  void: '#0a0d12',
  wallCap: '#2b3445',
  wallCapLight: '#3a4659',
  wallFace: '#47566e',
  wallFaceDark: '#3a475c',
  wallBase: '#2a3345',
  trim: '#6c7e99',
  glass: '#8fc7e8',
  glassDark: '#4b83a8',
  sky: '#a9dcf5',
  wood: '#8a5d3b',
  woodLight: '#a8754b',
  woodDark: '#5f3e27',
  metal: '#8b98a9',
  metalDark: '#4b5563',
  metalLight: '#b6c2d1',
  screenOff: '#10151c',
  screenOffEdge: '#1d2530',
  phosphor: '#38e8c6',
  phosphorDim: '#14705f',
  amber: '#f5b94a',
  paper: '#e9eef5',
  paperShade: '#b9c3d1',
  red: '#e0524f',
  green: '#4ac07a',
  leaf: '#3f9a52',
  leafLight: '#63c06f',
  leafDark: '#2a6e3b',
  pot: '#b8643c',
  potDark: '#8a4627',
} as const;
