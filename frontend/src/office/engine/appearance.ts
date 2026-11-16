/** Deterministic character appearance from an agent id. Pure. */

export const SKIN_TONES = ['#f2cba8', '#e3ab7c', '#c98c5c', '#a96b43', '#7b4a2d', '#f7dcc4'] as const;
export const HAIR_COLORS = ['#2a2019', '#5b3a22', '#8d5b2b', '#cfa24a', '#b23c2c', '#d9dae2', '#363c5c', '#7c3f9c'] as const;
export const OUTFIT_COLORS = ['#3f7fd9', '#2fb89a', '#d95f5f', '#e0a63a', '#8a63d2', '#d86fa8', '#5fa84a', '#e8e8ee', '#e07a3a', '#4aa6c8'] as const;
export const PANTS_COLORS = ['#2b3350', '#3b302b', '#4a505c', '#25403d', '#52394b'] as const;

export type HairStyle = 'short' | 'long' | 'cap' | 'spiky';
export type Accessory = 'none' | 'crown-tie' | 'glasses-scarf' | 'glasses' | 'headphones' | 'beret' | 'headband';

export interface Appearance {
  skin: string;
  hair: string;
  hairStyle: HairStyle;
  outfit: string;
  pants: string;
  accessory: Accessory;
  /** stable cache key */
  key: string;
}

/** FNV-1a 32-bit. */
export function hashString(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** Small deterministic PRNG (mulberry32). Returns a function yielding [0,1). */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const HAIR_STYLES: HairStyle[] = ['short', 'long', 'cap', 'spiky'];

function accessoryFor(role: string): Accessory {
  switch (role) {
    case 'leader':
      return 'crown-tie';
    case 'advisor':
      return 'glasses-scarf';
    case 'reviewer':
    case 'research':
      return 'glasses';
    case 'backend':
    case 'frontend':
      return 'headphones';
    case 'visual':
      return 'beret';
    case 'test':
      return 'headband';
    default:
      return 'none';
  }
}

export function appearanceFor(id: string, role: string): Appearance {
  const r = rng(hashString(id));
  const pick = <T>(arr: readonly T[]): T => arr[Math.floor(r() * arr.length)];
  const skin = pick(SKIN_TONES);
  const hair = pick(HAIR_COLORS);
  const hairStyle = pick(HAIR_STYLES);
  let outfit: string = pick(OUTFIT_COLORS);
  const pants = pick(PANTS_COLORS);
  const accessory = accessoryFor(role);
  // the leader and the advisor wear fixed, recognisable outfits
  if (role === 'leader') outfit = '#232a4e';
  if (role === 'advisor') outfit = '#5b3f91';
  return { skin, hair, hairStyle, outfit, pants, accessory, key: [skin, hair, hairStyle, outfit, pants, accessory].join('|') };
}
