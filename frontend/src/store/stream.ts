import type { ApiEvent, WsControl } from '../api/types';

/** `syncing` = socket open, replaying missed events; `live` only after the server's replay.done. */
export type ConnStatus = 'idle' | 'connecting' | 'syncing' | 'live' | 'reconnecting' | 'closed';

export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code?: number }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(code?: number): void;
}

export interface StreamOptions {
  /** Highest seq already applied; read on every (re)connect so resume is exact. */
  getAfterSeq: () => number;
  getToken: () => string | null;
  onEvents: (events: ApiEvent[]) => void;
  onStatus: (s: ConnStatus) => void;
  /** Called when the server rejects the token (close code 1008/4401). */
  onAuthFailure?: () => void;
  /** Called when the server rejects the Host/Origin (close code 4403); reconnecting cannot help. */
  onForbidden?: () => void;
  /** Called for control frames (replay.done, ping). */
  onControl?: (c: WsControl) => void;
  createSocket?: (url: string, protocols?: string[]) => SocketLike;
  baseUrl?: () => string;
  baseDelayMs?: number;
  maxDelayMs?: number;
}

export function defaultWsBase(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}/ws`;
}

/** `["orch", "orch.token.<token>"]`: the server echoes `orch` and checks the second entry. */
export function wsProtocols(token: string | null): string[] | undefined {
  return token ? ["orch", `orch.token.${token}`] : undefined;
}

export function backoffDelay(attempt: number, base = 500, max = 15000): number {
  return Math.min(max, base * 2 ** attempt);
}

/** 4401 bad token (also 1008 for proxies). 4403 is a Host/Origin rejection and is reported separately. */
const AUTH_CLOSE_CODES = new Set([1008, 4401]);
export const CLOSE_FORBIDDEN = 4403;
/** The server dropped us because our queue overflowed: reconnect right away with after_seq. */
export const CLOSE_TOO_SLOW = 4408;

export type Frame = { kind: 'event'; event: ApiEvent } | { kind: 'control'; control: WsControl } | { kind: 'ignored' };

/**
 * One WS text frame is either an event envelope ({seq, ts, type, ...payload}) or a control
 * message ({type: 'replay.done' | 'replay.truncated' | 'ping', seq}) from api/ws.py. Control frames are not events:
 * they carry a seq that is NOT an event seq and must never advance the applied seq.
 */
export function parseFrame(data: unknown): Frame {
  let v: unknown = data;
  if (typeof data === 'string') {
    try {
      v = JSON.parse(data);
    } catch {
      return { kind: 'ignored' };
    }
  }
  if (!v || typeof v !== 'object' || Array.isArray(v)) return { kind: 'ignored' };
  const o = v as Record<string, unknown>;
  if ((o.type === 'replay.done' || o.type === 'ping') && typeof o.seq === 'number' && !('ts' in o)) {
    if (o.type === 'replay.done' && o.truncated === true) return { kind: 'control', control: { type: o.type, seq: o.seq, truncated: true } };
    return { kind: 'control', control: { type: o.type, seq: o.seq } };
  }
  if (o.type === 'replay.truncated' && typeof o.seq === 'number' && !('ts' in o)) {
    const n = (k: string) => (typeof o[k] === 'number' ? (o[k] as number) : 0);
    return {
      kind: 'control',
      control: { type: o.type, seq: o.seq, requested_after_seq: n('requested_after_seq'), resume_after_seq: n('resume_after_seq'), skipped: n('skipped'), cap: n('cap') },
    };
  }
  if (typeof o.seq === 'number' && typeof o.type === 'string' && typeof o.ts === 'string') return { kind: 'event', event: v as ApiEvent };
  return { kind: 'ignored' };
}

export class EventStream {
  private sock: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = true;

  constructor(private readonly o: StreamOptions) {}

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const s = this.sock;
    this.sock = null;
    if (s) {
      s.onclose = null;
      s.onmessage = null;
      s.onerror = null;
      s.close();
    }
    this.o.onStatus('closed');
  }

  private url(): string {
    const base = (this.o.baseUrl ?? defaultWsBase)();
    // The token is never put in the URL (access logs, history): it travels as a WebSocket subprotocol.
    return `${base}?after_seq=${this.o.getAfterSeq()}`;
  }

  private connect(): void {
    if (this.stopped) return;
    this.o.onStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');
    const create = this.o.createSocket ?? ((u: string, p?: string[]) => new WebSocket(u, p) as unknown as SocketLike);
    let sock: SocketLike;
    try {
      sock = create(this.url(), wsProtocols(this.o.getToken()));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.sock = sock;
    sock.onopen = () => {
      this.attempt = 0;
      this.o.onStatus('syncing');
    };
    sock.onmessage = (m) => {
      const f = parseFrame(m.data);
      if (f.kind === 'event') this.o.onEvents([f.event]);
      else if (f.kind === 'control') {
        if (f.control.type === 'replay.done') this.o.onStatus('live');
        this.o.onControl?.(f.control);
      }
    };
    sock.onerror = () => {
      /* close follows; handled there */
    };
    sock.onclose = (ev) => {
      this.sock = null;
      if (this.stopped) return;
      if (ev?.code !== undefined && AUTH_CLOSE_CODES.has(ev.code)) {
        this.stopped = true;
        this.o.onStatus('closed');
        this.o.onAuthFailure?.();
        return;
      }
      if (ev?.code === CLOSE_FORBIDDEN) {
        this.stopped = true;
        this.o.onStatus('closed');
        this.o.onForbidden?.();
        return;
      }
      if (ev?.code === CLOSE_TOO_SLOW) {
        this.attempt = 0;
        this.scheduleReconnect(0); // resume from the last applied seq straight away
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(delayOverride?: number): void {
    if (this.stopped) return;
    this.o.onStatus('reconnecting');
    const delay = delayOverride ?? backoffDelay(this.attempt, this.o.baseDelayMs, this.o.maxDelayMs);
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }
}
