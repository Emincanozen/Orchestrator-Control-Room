/**
 * PixiJS renderer for the pixel office. All art is generated in code
 * (canvas 2D -> cached textures); the render loop only reads the simulation:
 * no network, no store writes, no per-frame Graphics rebuilds.
 */
import { Application, Container, Rectangle, Sprite, Texture, TextureSource } from 'pixi.js';
import { ACTIVITY_COLOR } from '../../design/tokens';
import type { OfficeEngine, OfficeEngineOptions } from './api';
import { labelFor } from './behavior';
import { buildCharacterAtlas, CELL_H, CELL_W, FEET_Y } from './draw/characters';
import { furnitureSprite, spriteKey, SPRITE_SIZE, type SpriteState } from './draw/furniture';
import { drawChevron, drawIndicator, drawRing, drawShadow, ICON_SIZE } from './draw/indicators';
import { bakeWorld } from './draw/world';
import { LAYOUT, type FurnitureItem } from './layout';
import { OfficeSim, type AgentRender } from './sim';
import { frameKey, TILE, type Indicator, type OfficeAgent } from './types';

const MAP_W = LAYOUT.cols * TILE;
const MAP_H = LAYOUT.rows * TILE;
const MAX_SCALE = 6;
const BODY_ANCHOR_Y = FEET_Y / CELL_H;

const makeCanvas = (w: number, h: number): HTMLCanvasElement => {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  return c;
};

const texFromCanvas = (cv: HTMLCanvasElement): Texture => {
  const t = Texture.from(cv);
  t.source.scaleMode = 'nearest';
  return t;
};

interface AgentView {
  id: string;
  src: OfficeAgent;
  atlasTex: Texture;
  frames: Map<number, Texture>;
  index: Map<string, number>;
  cols: number;
  body: Sprite;
  shadow: Sprite;
  ring: Sprite;
  chevron: Sprite;
  icon: Sprite;
  iconKey: string;
  frameKey: string;
  label: LabelEl;
  last: AgentRender | null;
}

interface LabelEl {
  root: HTMLDivElement;
  name: HTMLSpanElement;
  dot: HTMLSpanElement;
  model: HTMLDivElement;
  lastTransform: string;
}

interface DynamicFurniture {
  sprite: Sprite;
  item: FurnitureItem;
  key: string;
}

interface Particle {
  sprite: Sprite;
  vx: number;
  vy: number;
  life: number;
}

const CONFETTI = [0xf472b6, 0x38e8c6, 0xf5b94a, 0x6aa8ff, 0xfacc15, 0xa78bfa];

export class PixiOffice implements OfficeEngine {
  private readonly sim: OfficeSim;
  private app: Application | null = null;
  private destroyed = false;
  private ready = false;
  private agents: OfficeAgent[] = [];
  private selectedId: string | null = null;
  private hoverId: string | null = null;
  private reduced: boolean;

  private world!: Container;
  private ground!: Container;
  private things!: Container;
  private top!: Container;
  private fx!: Container;
  private labelRoot!: HTMLDivElement;

  private views = new Map<string, AgentView>();
  private dynamic: DynamicFurniture[] = [];
  private texCache = new Map<string, Texture>();
  private iconTex = new Map<string, Texture>();
  private shadowTex!: Texture;
  private ringTex!: Texture;
  private ringHoverTex!: Texture;
  private chevronTex!: Texture;
  private particles: Particle[] = [];
  private spawnClock = 0;

  private userScale: number | null = null;
  private scale = 1;
  private cx = MAP_W / 2;
  private cy = MAP_H / 2;
  private clock = 0;
  private cleanups: (() => void)[] = [];
  private lastWheel = 0;

  constructor(private readonly opts: OfficeEngineOptions) {
    this.reduced = opts.reducedMotion;
    this.sim = new OfficeSim({ reducedMotion: this.reduced });
  }

  // -------------------------------------------------------------- lifecycle

  async mount(host: HTMLElement): Promise<void> {
    const app = new Application();
    this.app = app;
    try {
      TextureSource.defaultOptions.scaleMode = 'nearest';
      await app.init({
        resizeTo: host,
        backgroundAlpha: 0,
        antialias: false,
        roundPixels: true,
        resolution: Math.min(window.devicePixelRatio || 1, 3),
        autoDensity: true,
        preference: 'webgl',
        autoStart: false,
      });
    } catch (e) {
      this.app = null;
      if (!this.destroyed) this.opts.onError?.(e instanceof Error ? e.message : String(e));
      return;
    }
    if (this.destroyed) {
      this.disposeApp(app);
      return;
    }
    this.buildScene(app, host);
    this.ready = true;
    this.reconcileViews();
    this.updateCamera();
    this.render(0);
    if (!document.hidden) app.ticker.start();
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.ready = false;
    for (const fn of this.cleanups) fn();
    this.cleanups = [];
    this.labelRoot?.remove();
    const app = this.app;
    this.app = null;
    // if init is still pending the mount() continuation disposes the app
    if (app && this.world) this.disposeApp(app);
    this.views.clear();
    this.dynamic = [];
    this.texCache.clear();
    this.particles = [];
  }

  private disposeApp(app: Application): void {
    try {
      app.ticker.stop();
      app.destroy({ removeView: true }, { children: true, texture: true, textureSource: true });
    } catch {
      /* already destroyed */
    }
  }

  // ----------------------------------------------------------------- public

  setAgents(agents: OfficeAgent[]): void {
    this.agents = agents;
    this.sim.sync(agents);
    if (this.ready) this.reconcileViews();
  }

  setSelected(id: string | null): void {
    this.selectedId = id;
  }

  setReducedMotion(v: boolean): void {
    this.reduced = v;
    this.sim.setReducedMotion(v);
  }

  zoomIn(): void {
    this.userScale = Math.min(MAX_SCALE, this.scale + 1);
    this.updateCamera();
  }
  zoomOut(): void {
    this.userScale = Math.max(1, this.scale - 1);
    this.updateCamera();
  }
  fit(): void {
    this.userScale = null;
    this.cx = MAP_W / 2;
    this.cy = MAP_H / 2;
    this.updateCamera();
  }

  // ------------------------------------------------------------------ scene

  private buildScene(app: Application, host: HTMLElement): void {
    const canvas = app.canvas;
    canvas.style.imageRendering = 'pixelated';
    canvas.style.display = 'block';
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', this.opts.ariaLabel);
    canvas.setAttribute('data-testid', 'office-canvas');
    host.appendChild(canvas);

    this.world = new Container();
    this.ground = new Container();
    this.things = new Container();
    this.things.sortableChildren = true;
    this.top = new Container();
    this.fx = new Container();
    this.world.addChild(this.ground, this.things, this.top, this.fx);
    app.stage.addChild(this.world);

    // baked floor / walls / decor: one texture, drawn once
    const bake = makeCanvas(MAP_W, MAP_H);
    bakeWorld(bake.getContext('2d') as CanvasRenderingContext2D);
    const floor = new Sprite(texFromCanvas(bake));
    this.ground.addChild(floor);

    // furniture (depth-sorted with the characters through zIndex)
    for (const item of LAYOUT.furniture) {
      const { h } = SPRITE_SIZE[item.kind];
      const state = this.initialState(item);
      const s = new Sprite(this.furnTexture(item, state));
      s.position.set(item.x * TILE, (item.y + item.h) * TILE - h);
      s.zIndex = item.blocks ? (item.y + item.h) * TILE : item.y * TILE + 8;
      this.things.addChild(s);
      if (item.kind === 'desk' || item.kind === 'testbench' || (item.kind === 'rack' && item.room === 'testing')) {
        this.dynamic.push({ sprite: s, item, key: spriteKey(item, state) });
      }
    }

    const mk = (draw: (c: CanvasRenderingContext2D) => void, w: number, h: number) => {
      const cv = makeCanvas(w, h);
      draw(cv.getContext('2d', { willReadFrequently: true }) as CanvasRenderingContext2D);
      return texFromCanvas(cv);
    };
    this.shadowTex = mk(drawShadow, 12, 4);
    this.ringTex = mk((c) => drawRing(c, '#38e8c6'), 24, 10);
    this.ringHoverTex = mk((c) => drawRing(c, '#e9eef5'), 24, 10);
    this.chevronTex = mk((c) => drawChevron(c, '#38e8c6'), 11, 7);

    // DOM label overlay
    this.labelRoot = document.createElement('div');
    this.labelRoot.setAttribute('aria-hidden', 'true');
    Object.assign(this.labelRoot.style, { position: 'absolute', inset: '0', overflow: 'hidden', pointerEvents: 'none' } satisfies Partial<CSSStyleDeclaration>);
    host.appendChild(this.labelRoot);

    this.attachInput(canvas);
    app.ticker.add((t) => {
      const dt = Math.min(t.deltaMS / 1000, 0.1);
      this.render(dt);
    });
    const onVis = () => {
      if (!this.app) return;
      if (document.hidden) this.app.ticker.stop();
      else this.app.ticker.start();
    };
    document.addEventListener('visibilitychange', onVis);
    this.cleanups.push(() => document.removeEventListener('visibilitychange', onVis));
    const ro = new ResizeObserver(() => {
      this.app?.resize();
      this.updateCamera();
    });
    ro.observe(host);
    this.cleanups.push(() => ro.disconnect());
  }

  private initialState(item: FurnitureItem): SpriteState {
    if (item.kind === 'desk') return { mode: 'off', frame: 0 };
    if (item.kind === 'rack' || item.kind === 'testbench') return { mode: 'off', frame: 0 };
    return {};
  }

  private furnTexture(item: FurnitureItem, st: SpriteState): Texture {
    const key = spriteKey(item, st);
    let t = this.texCache.get(key);
    if (!t) {
      t = texFromCanvas(furnitureSprite(item, st, makeCanvas));
      this.texCache.set(key, t);
    }
    return t;
  }

  private iconTexture(kind: Indicator, frame: number): Texture {
    const key = `${kind}:${frame}`;
    let t = this.iconTex.get(key);
    if (!t) {
      const { w, h } = ICON_SIZE[kind];
      const cv = makeCanvas(w, h);
      drawIndicator(cv.getContext('2d', { willReadFrequently: true }) as CanvasRenderingContext2D, kind, frame);
      t = texFromCanvas(cv);
      this.iconTex.set(key, t);
    }
    return t;
  }

  // ------------------------------------------------------------ agent views

  private reconcileViews(): void {
    const ids = new Set(this.agents.map((a) => a.id));
    for (const [id, v] of this.views) {
      if (!ids.has(id)) {
        v.body.destroy();
        v.shadow.destroy();
        v.ring.destroy();
        v.chevron.destroy();
        v.icon.destroy();
        v.label.root.remove();
        this.views.delete(id);
      }
    }
    for (const a of this.agents) {
      let v = this.views.get(a.id);
      if (!v) {
        v = this.createView(a);
        this.views.set(a.id, v);
      }
      v.src = a;
      this.updateLabelText(v);
    }
  }

  private createView(a: OfficeAgent): AgentView {
    const ap = this.sim.appearanceOf(a.id)!;
    const atlas = buildCharacterAtlas(ap, makeCanvas);
    const atlasTex = texFromCanvas(atlas.canvas as HTMLCanvasElement);
    const body = new Sprite();
    body.anchor.set(0.5, BODY_ANCHOR_Y);
    this.things.addChild(body);
    const shadow = new Sprite(this.shadowTex);
    this.ground.addChild(shadow);
    const ring = new Sprite(this.ringTex);
    ring.visible = false;
    this.ground.addChild(ring);
    const chevron = new Sprite(this.chevronTex);
    chevron.anchor.set(0.5, 1);
    chevron.visible = false;
    this.top.addChild(chevron);
    const icon = new Sprite();
    icon.anchor.set(0.5, 1);
    icon.visible = false;
    this.top.addChild(icon);
    return {
      id: a.id,
      src: a,
      atlasTex,
      frames: new Map(),
      index: atlas.index,
      cols: atlas.cols,
      body,
      shadow,
      ring,
      chevron,
      icon,
      iconKey: '',
      frameKey: '',
      label: this.createLabel(),
      last: null,
    };
  }

  private frameTexture(v: AgentView, key: string): Texture | null {
    const i = v.index.get(key);
    if (i === undefined) return null;
    let t = v.frames.get(i);
    if (!t) {
      t = new Texture({ source: v.atlasTex.source, frame: new Rectangle((i % v.cols) * CELL_W, Math.floor(i / v.cols) * CELL_H, CELL_W, CELL_H) });
      v.frames.set(i, t);
    }
    return t;
  }

  // ----------------------------------------------------------------- labels

  private createLabel(): LabelEl {
    const root = document.createElement('div');
    Object.assign(root.style, {
      position: 'absolute',
      left: '0',
      top: '0',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      gap: '1px',
      font: '600 10px/1.2 var(--font-mono, ui-monospace, Consolas, monospace)',
      whiteSpace: 'nowrap',
      willChange: 'transform',
    } satisfies Partial<CSSStyleDeclaration>);
    const nameRow = document.createElement('div');
    Object.assign(nameRow.style, {
      display: 'flex',
      alignItems: 'center',
      gap: '4px',
      padding: '0 4px',
      background: 'rgba(10,13,18,0.86)',
      border: '1px solid #34414f',
      color: '#d9e2ec',
    } satisfies Partial<CSSStyleDeclaration>);
    const dot = document.createElement('span');
    Object.assign(dot.style, { width: '6px', height: '6px', display: 'inline-block', flex: 'none' } satisfies Partial<CSSStyleDeclaration>);
    const name = document.createElement('span');
    nameRow.append(dot, name);
    const model = document.createElement('div');
    Object.assign(model.style, {
      maxWidth: '124px',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      padding: '0 3px',
      fontSize: '9px',
      fontWeight: '500',
      background: 'rgba(10,13,18,0.78)',
      color: '#8493a5',
    } satisfies Partial<CSSStyleDeclaration>);
    root.append(nameRow, model);
    this.labelRoot.appendChild(root);
    return { root, name, dot, model, lastTransform: '' };
  }

  private updateLabelText(v: AgentView): void {
    const t = labelFor(v.src);
    v.label.name.textContent = t.name;
    v.label.dot.style.background = ACTIVITY_COLOR[v.src.activity as keyof typeof ACTIVITY_COLOR] ?? 'var(--color-muted)';
    v.label.model.textContent = t.line ?? '';
    v.label.model.dataset.has = t.line ? '1' : '0';
    v.label.root.title = `${t.name} · ${v.src.activity.replace(/_/g, ' ')}${t.line ? ` · ${t.line}` : ''}`;
  }

  // ----------------------------------------------------------------- camera

  private fitScale(): number {
    const s = this.app!.screen;
    return Math.max(1, Math.floor(Math.min(s.width / MAP_W, s.height / MAP_H) + 0.25));
  }

  private updateCamera(): void {
    if (!this.app || !this.ready) return;
    const vw = this.app.screen.width;
    const vh = this.app.screen.height;
    const prev = this.scale;
    this.scale = this.userScale ?? this.fitScale();
    const s = this.scale;
    const place = (view: number, map: number, c: number): [number, number] => {
      const size = map * s;
      if (size <= view) return [Math.round((view - size) / 2), map / 2];
      const o = Math.min(0, Math.max(view - size, Math.round(view / 2 - c * s)));
      return [o, (view / 2 - o) / s];
    };
    const [ox, ncx] = place(vw, MAP_W, this.cx);
    const [oy, ncy] = place(vh, MAP_H, this.cy);
    this.cx = ncx;
    this.cy = ncy;
    this.world.scale.set(s);
    this.world.position.set(ox, oy);
    if (prev !== s) this.opts.onZoom?.(s);
  }

  private toWorld(clientX: number, clientY: number): { x: number; y: number } {
    const rect = this.app!.canvas.getBoundingClientRect();
    return { x: (clientX - rect.left - this.world.x) / this.scale, y: (clientY - rect.top - this.world.y) / this.scale };
  }

  // ------------------------------------------------------------------ input

  private hitTest(wx: number, wy: number): string | null {
    let best: { id: string; y: number } | null = null;
    for (const v of this.views.values()) {
      const r = v.last;
      if (!r) continue;
      if (wx >= r.x - 9 && wx <= r.x + 9 && wy >= r.y + r.jump - 24 && wy <= r.y + 2) {
        if (!best || r.y > best.y) best = { id: v.id, y: r.y };
      }
    }
    return best?.id ?? null;
  }

  private attachInput(canvas: HTMLCanvasElement): void {
    let drag: { x: number; y: number; cx: number; cy: number; moved: boolean } | null = null;
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, cx: this.cx, cy: this.cy, moved: false };
      try {
        canvas.setPointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
    };
    const move = (e: PointerEvent) => {
      if (drag) {
        const dx = e.clientX - drag.x;
        const dy = e.clientY - drag.y;
        if (!drag.moved && Math.hypot(dx, dy) > 4) drag.moved = true;
        if (drag.moved) {
          this.cx = drag.cx - dx / this.scale;
          this.cy = drag.cy - dy / this.scale;
          canvas.style.cursor = 'grabbing';
          this.updateCamera();
          return;
        }
      }
      const p = this.toWorld(e.clientX, e.clientY);
      this.hoverId = this.hitTest(p.x, p.y);
      canvas.style.cursor = this.hoverId ? 'pointer' : 'default';
    };
    const up = (e: PointerEvent) => {
      const d = drag;
      drag = null;
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      if (!d || d.moved) {
        canvas.style.cursor = this.hoverId ? 'pointer' : 'default';
        return;
      }
      const p = this.toWorld(e.clientX, e.clientY);
      const id = this.hitTest(p.x, p.y);
      if (id) this.opts.onSelect(id);
    };
    const leave = () => {
      this.hoverId = null;
      canvas.style.cursor = 'default';
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      const now = performance.now();
      if (now - this.lastWheel < 120) return;
      this.lastWheel = now;
      if (e.deltaY < 0) this.zoomIn();
      else this.zoomOut();
    };
    canvas.addEventListener('pointerdown', down);
    canvas.addEventListener('pointermove', move);
    canvas.addEventListener('pointerup', up);
    canvas.addEventListener('pointerleave', leave);
    canvas.addEventListener('wheel', wheel, { passive: false });
    this.cleanups.push(() => {
      canvas.removeEventListener('pointerdown', down);
      canvas.removeEventListener('pointermove', move);
      canvas.removeEventListener('pointerup', up);
      canvas.removeEventListener('pointerleave', leave);
      canvas.removeEventListener('wheel', wheel);
    });
  }

  // ------------------------------------------------------------------ frame

  private render(dt: number): void {
    if (!this.ready) return;
    this.clock += dt;
    this.sim.update(dt);
    const renders = this.sim.renders();
    this.updateDynamicFurniture();
    for (const r of renders) {
      const v = this.views.get(r.id);
      if (v) this.updateView(v, r, dt);
    }
    this.updateParticles(dt);
    this.layoutLabels();
  }

  private updateDynamicFurniture(): void {
    const screens = this.sim.deskScreens();
    const station = this.sim.stationActive();
    const fr3 = this.reduced ? 0 : Math.floor(this.clock * 3) % 3;
    const fr2 = this.reduced ? 0 : Math.floor(this.clock * 4) % 2;
    for (const d of this.dynamic) {
      let st: SpriteState;
      if (d.item.kind === 'desk') {
        const mode = (d.item.seatId && screens.get(d.item.seatId)) || 'off';
        st = { mode, frame: mode === 'off' ? 0 : fr3 };
      } else {
        st = station ? { mode: 'lit', frame: fr2 } : { mode: 'off', frame: 0 };
      }
      const key = spriteKey(d.item, st);
      if (key !== d.key) {
        d.key = key;
        d.sprite.texture = this.furnTexture(d.item, st);
      }
    }
  }

  private updateView(v: AgentView, r: AgentRender, dt: number): void {
    v.last = r;
    const x = Math.round(r.x);
    const y = Math.round(r.y);
    const fk = frameKey({ pose: r.pose, dir: r.dir, anim: r.anim, f: r.frame });
    if (fk !== v.frameKey) {
      const t = this.frameTexture(v, fk) ?? this.frameTexture(v, frameKey({ pose: r.pose, dir: r.dir, anim: 'idle', f: 0 }));
      if (t) v.body.texture = t;
      v.frameKey = fk;
    }
    v.body.position.set(x, y + Math.round(r.jump));
    v.body.zIndex = y;
    v.body.alpha = r.dim ? 0.55 : 1;
    v.body.tint = r.dim ? 0x9aa2bd : 0xffffff;
    v.shadow.position.set(x - 6, y - 3);
    v.shadow.alpha = r.jump < 0 ? 0.6 : 1;

    const selected = v.id === this.selectedId;
    const hover = v.id === this.hoverId;
    v.ring.visible = selected || hover;
    if (v.ring.visible) {
      v.ring.texture = selected ? this.ringTex : this.ringHoverTex;
      v.ring.position.set(x - 12, y - 6);
    }
    v.chevron.visible = selected;
    if (selected) {
      const bob = this.reduced ? 0 : Math.round(Math.sin(this.clock * 4));
      v.chevron.position.set(x, y - (r.pose === 'sit' ? 25 : 29) - 10 + bob);
    }

    // overlay indicator
    if (r.indicator) {
      const frames = ICON_SIZE[r.indicator].frames;
      const frame = r.indicator === 'speech' || r.indicator === 'sleep' ? (this.reduced ? 0 : Math.floor(this.clock * (r.indicator === 'speech' ? 3 : 1)) % frames) : 0;
      const key = `${r.indicator}:${frame}`;
      if (key !== v.iconKey) {
        v.icon.texture = this.iconTexture(r.indicator, frame);
        v.iconKey = key;
      }
      const pulse = this.reduced ? 0 : Math.sin(this.clock * (r.indicator === 'approval' ? 7 : 4));
      v.icon.visible = true;
      v.icon.alpha = r.indicator === 'approval' || r.indicator === 'blocked' ? 0.78 + 0.22 * (pulse * 0.5 + 0.5) : 1;
      const bob = r.indicator === 'approval' ? Math.round(pulse) : 0;
      const shake = r.indicator === 'warning' ? Math.round(pulse) : 0;
      v.icon.position.set(x + (r.indicator === 'speech' ? 6 : 0) + shake, y - (r.pose === 'sit' ? 22 : 26) + Math.round(r.jump) - 1 + bob);
    } else {
      v.icon.visible = false;
      v.iconKey = '';
    }

    // confetti
    if (r.celebrating && !this.reduced) {
      this.spawnClock += dt;
      while (this.spawnClock > 0.035 && this.particles.length < 90) {
        this.spawnClock -= 0.035;
        this.spawnConfetti(x, y - 18);
      }
    }
  }

  /** Positions the DOM labels below the feet; labels of agents standing close together are stacked. */
  private layoutLabels(): void {
    const rect = this.app!.screen;
    const list = [...this.views.values()].filter((v) => v.last).sort((p, q) => p.last!.x - q.last!.x || (p.id < q.id ? -1 : 1));
    const wWorld = 64 / this.scale;
    for (let i = 0; i < list.length; i++) {
      const v = list[i];
      const r = v.last!;
      let stack = 0;
      for (let j = 0; j < i; j++) {
        const o = list[j].last!;
        if (Math.abs(o.x - r.x) < wWorld && Math.abs(o.y - r.y) < 6) stack++;
      }
      const selected = v.id === this.selectedId;
      const hover = v.id === this.hoverId;
      const el = v.label;
      const sx = Math.round(this.world.x + Math.round(r.x) * this.scale);
      const sy = Math.round(this.world.y + (Math.round(r.y) + 3) * this.scale) + stack * (this.scale >= 2 ? 26 : 13);
      const tf = `translate(${sx}px, ${sy}px) translateX(-50%)`;
      if (tf !== el.lastTransform) {
        el.root.style.transform = tf;
        el.lastTransform = tf;
      }
      const emph = selected || hover;
      const showModel = el.model.dataset.has === '1' && ((this.scale >= 2 && stack === 0) || emph);
      el.model.style.display = showModel ? 'block' : 'none';
      el.model.style.maxWidth = emph ? '220px' : `${Math.max(60, 46 * this.scale - 4)}px`;
      el.root.style.zIndex = emph ? '2' : '1';
      (el.root.firstElementChild as HTMLElement).style.borderColor = selected ? '#38e8c6' : hover ? '#d9e2ec' : '#34414f';
      el.root.style.visibility = sx < -80 || sy < -40 || sx > rect.width + 80 || sy > rect.height + 20 ? 'hidden' : 'visible';
    }
  }

  private spawnConfetti(x: number, y: number): void {
    const s = new Sprite(Texture.WHITE);
    s.width = 2;
    s.height = 2;
    s.tint = CONFETTI[Math.floor(Math.random() * CONFETTI.length)];
    s.position.set(x, y);
    this.fx.addChild(s);
    this.particles.push({ sprite: s, vx: (Math.random() - 0.5) * 60, vy: -30 - Math.random() * 50, life: 0.9 + Math.random() * 0.6 });
  }

  private updateParticles(dt: number): void {
    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.life -= dt;
      if (p.life <= 0) {
        p.sprite.destroy();
        this.particles.splice(i, 1);
        continue;
      }
      p.vy += 140 * dt;
      p.sprite.x = Math.round(p.sprite.x + p.vx * dt);
      p.sprite.y = Math.round(p.sprite.y + p.vy * dt);
      p.sprite.alpha = Math.min(1, p.life * 2);
    }
  }
}

export function createPixiOffice(opts: OfficeEngineOptions): OfficeEngine {
  return new PixiOffice(opts);
}
