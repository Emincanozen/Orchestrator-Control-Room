/** Grid A* (4-neighbour). Pure and Pixi-free. */
import type { Tile } from './types';

export interface PathGrid {
  cols: number;
  rows: number;
  walkable: boolean[][];
  /** optional extra cost per tile (>= 1) */
  cost?: number[][];
}

/**
 * Returns the tiles to walk through (excluding `from`, including `to`),
 * `[]` when from === to, or `null` when `to` is unreachable / blocked.
 */
export function findPath(grid: PathGrid, from: Tile, to: Tile): Tile[] | null {
  const inside = (x: number, y: number) => x >= 0 && y >= 0 && x < grid.cols && y < grid.rows;
  if (!inside(from.x, from.y) || !inside(to.x, to.y)) return null;
  if (!grid.walkable[to.y][to.x]) return null;
  if (from.x === to.x && from.y === to.y) return [];
  const key = (x: number, y: number) => y * grid.cols + x;
  const g = new Map<number, number>();
  const prev = new Map<number, number>();
  const open: { k: number; f: number; x: number; y: number }[] = [];
  const closed = new Set<number>();
  const h = (x: number, y: number) => Math.abs(x - to.x) + Math.abs(y - to.y);
  const startKey = key(from.x, from.y);
  g.set(startKey, 0);
  open.push({ k: startKey, f: h(from.x, from.y), x: from.x, y: from.y });
  const DIRS = [
    [0, -1],
    [1, 0],
    [0, 1],
    [-1, 0],
  ];
  while (open.length) {
    let bi = 0;
    for (let i = 1; i < open.length; i++) {
      if (open[i].f < open[bi].f || (open[i].f === open[bi].f && open[i].k < open[bi].k)) bi = i;
    }
    const cur = open.splice(bi, 1)[0];
    if (closed.has(cur.k)) continue;
    closed.add(cur.k);
    if (cur.x === to.x && cur.y === to.y) {
      const out: Tile[] = [];
      let k = cur.k;
      while (k !== startKey) {
        out.push({ x: k % grid.cols, y: Math.floor(k / grid.cols) });
        k = prev.get(k)!;
      }
      return out.reverse();
    }
    const gc = g.get(cur.k)!;
    for (const [dx, dy] of DIRS) {
      const nx = cur.x + dx;
      const ny = cur.y + dy;
      if (!inside(nx, ny) || !grid.walkable[ny][nx]) continue;
      const nk = key(nx, ny);
      if (closed.has(nk)) continue;
      const ng = gc + (grid.cost?.[ny][nx] ?? 1);
      if (ng < (g.get(nk) ?? Infinity)) {
        g.set(nk, ng);
        prev.set(nk, cur.k);
        open.push({ k: nk, f: ng + h(nx, ny), x: nx, y: ny });
      }
    }
  }
  return null;
}
