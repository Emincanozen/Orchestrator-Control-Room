import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventStream, backoffDelay, parseFrame, type ConnStatus, type SocketLike } from './stream';
import { ev } from '../test/fixtures';
import { applyEvents, applySnapshot, initialData } from './reducer';

class FakeSocket implements SocketLike {
  onopen: SocketLike['onopen'] = null;
  onmessage: SocketLike['onmessage'] = null;
  onclose: SocketLike['onclose'] = null;
  onerror: SocketLike['onerror'] = null;
  closed = false;
  constructor(public url: string, public protocols?: string[]) {}
  close() {
    this.closed = true;
  }
}

describe('parseFrame', () => {
  it('distinguishes events from control frames and drops junk', () => {
    const e = ev(1, 'task.created');
    expect(parseFrame(JSON.stringify(e))).toEqual({ kind: 'event', event: e });
    expect(parseFrame(JSON.stringify({ type: 'replay.done', seq: 9 }))).toEqual({ kind: 'control', control: { type: 'replay.done', seq: 9 } });
    expect(parseFrame(JSON.stringify({ type: 'ping', seq: 9 }))).toEqual({ kind: 'control', control: { type: 'ping', seq: 9 } });
    expect(parseFrame('not json').kind).toBe('ignored');
    expect(parseFrame(JSON.stringify({ hello: 1 })).kind).toBe('ignored');
    expect(parseFrame(JSON.stringify([e])).kind).toBe('ignored');
  });
});

describe('truncated replay frames', () => {
  it('parses replay.truncated and replay.done with truncated', () => {
    const t = { type: 'replay.truncated', requested_after_seq: 3, resume_after_seq: 90, skipped: 87, cap: 500, seq: 90 };
    expect(parseFrame(JSON.stringify(t))).toEqual({ kind: 'control', control: t });
    expect(parseFrame(JSON.stringify({ type: 'replay.done', seq: 600, truncated: true }))).toEqual({ kind: 'control', control: { type: 'replay.done', seq: 600, truncated: true } });
    // a control frame never carries ts; with ts it would be an event envelope
    expect(parseFrame(JSON.stringify({ ...t, ts: '2026-01-01T00:00:00Z' })).kind).toBe('event');
  });

  it('passes control frames on without advancing the applied seq and goes live on replay.done', () => {
    const sockets: FakeSocket[] = [];
    const controls: string[] = [];
    const statuses: ConnStatus[] = [];
    const stream = new EventStream({
      getAfterSeq: () => 3,
      getToken: () => null,
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: (s) => statuses.push(s),
      onControl: (c) => controls.push(c.type),
      createSocket: (u, p) => {
        const s = new FakeSocket(u, p);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    expect(sockets[0].protocols).toBeUndefined(); // no token, no subprotocol
    sockets[0].onopen?.({});
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'replay.truncated', requested_after_seq: 3, resume_after_seq: 90, skipped: 87, cap: 500, seq: 90 }) });
    expect(statuses.at(-1)).toBe('syncing');
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'replay.done', seq: 600, truncated: true }) });
    expect(statuses.at(-1)).toBe('live');
    expect(controls).toEqual(['replay.truncated', 'replay.done']);
    stream.stop();
  });
});

describe('EventStream reconnect', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('reconnects with exponential backoff and resumes after the last applied seq', () => {
    let state = applySnapshot(initialData(), { projects: [], tasks: [], agents: [], approvals: [], lastSeq: 5 });
    const sockets: FakeSocket[] = [];
    const statuses: ConnStatus[] = [];
    const stream = new EventStream({
      getAfterSeq: () => state.lastSeq,
      getToken: () => 'tok',
      baseUrl: () => 'ws://x/ws',
      onEvents: (evs) => {
        state = applyEvents(state, evs);
      },
      onStatus: (s) => statuses.push(s),
      createSocket: (u, p) => {
        const s = new FakeSocket(u, p);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    expect(sockets[0].url).toBe('ws://x/ws?after_seq=5'); // the token never appears in the URL
    expect(sockets[0].protocols).toEqual(['orch', 'orch.token.tok']);
    sockets[0].onopen?.({});
    expect(statuses.at(-1)).toBe('syncing');
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'replay.done', seq: 5 }) });
    expect(statuses.at(-1)).toBe('live');
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ping', seq: 5 }) });
    expect(state.lastSeq).toBe(5); // control frames never advance the applied seq
    sockets[0].onmessage?.({ data: JSON.stringify(ev(6, 'system.notice', { payload: { message: 'a' } })) });
    sockets[0].onmessage?.({ data: JSON.stringify(ev(7, 'system.notice', { payload: { message: 'b' } })) });
    expect(state.lastSeq).toBe(7);

    sockets[0].onclose?.({ code: 1006 });
    expect(statuses.at(-1)).toBe('reconnecting');
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(backoffDelay(0) - 1);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);
    expect(sockets[1].url).toContain('after_seq=7'); // resume from last seq

    // second failure backs off longer
    sockets[1].onclose?.({ code: 1006 });
    vi.advanceTimersByTime(backoffDelay(1) - 1);
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(3);

    // server replays overlapping events: duplicates are ignored
    sockets[2].onopen?.({});
    sockets[2].onmessage?.({ data: JSON.stringify(ev(7, 'system.notice')) });
    sockets[2].onmessage?.({ data: JSON.stringify(ev(8, 'system.notice', { payload: { message: 'c' } })) });
    expect(state.lastSeq).toBe(8);
    expect(state.notices.map((n) => n.message)).toEqual(['a', 'b', 'c']);
    stream.stop();
  });

  it('resets backoff after a successful open', () => {
    const sockets: FakeSocket[] = [];
    const stream = new EventStream({
      getAfterSeq: () => 0,
      getToken: () => null,
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: () => undefined,
      createSocket: (u) => {
        const s = new FakeSocket(u);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    sockets[0].onclose?.({ code: 1006 });
    vi.advanceTimersByTime(backoffDelay(0));
    sockets[1].onopen?.({});
    sockets[1].onclose?.({ code: 1006 });
    vi.advanceTimersByTime(backoffDelay(0));
    expect(sockets).toHaveLength(3);
    stream.stop();
  });

  it('stops and reports auth failure on policy-violation close', () => {
    const onAuthFailure = vi.fn();
    const sockets: FakeSocket[] = [];
    const stream = new EventStream({
      getAfterSeq: () => 0,
      getToken: () => 'bad',
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: () => undefined,
      onAuthFailure,
      createSocket: (u) => {
        const s = new FakeSocket(u);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    sockets[0].onclose?.({ code: 1008 });
    vi.advanceTimersByTime(60_000);
    expect(onAuthFailure).toHaveBeenCalledOnce();
    expect(sockets).toHaveLength(1);
  });

  it('reconnects immediately with the last seq on 4408 (client too slow)', () => {
    const sockets: FakeSocket[] = [];
    let last = 12;
    const stream = new EventStream({
      getAfterSeq: () => last,
      getToken: () => 't',
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: () => undefined,
      createSocket: (u) => {
        const s = new FakeSocket(u);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    sockets[0].onclose?.({ code: 4408 });
    vi.advanceTimersByTime(0);
    expect(sockets).toHaveLength(2);
    last = 20;
    sockets[1].onclose?.({ code: 4408 });
    vi.advanceTimersByTime(0);
    expect(sockets[2].url).toContain('after_seq=20');
    stream.stop();
  });

  it('4403 (bad Host/Origin) stops retrying and is not reported as an auth failure', () => {
    const onAuthFailure = vi.fn();
    const onForbidden = vi.fn();
    const sockets: FakeSocket[] = [];
    const stream = new EventStream({
      getAfterSeq: () => 0,
      getToken: () => 't',
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: () => undefined,
      onAuthFailure,
      onForbidden,
      createSocket: (u) => {
        const s = new FakeSocket(u);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    sockets[0].onclose?.({ code: 4403 });
    vi.advanceTimersByTime(60_000);
    expect(onForbidden).toHaveBeenCalledOnce();
    expect(onAuthFailure).not.toHaveBeenCalled();
    expect(sockets).toHaveLength(1);
  });

  it('4401 reports an auth failure', () => {
    const onAuthFailure = vi.fn();
    const sockets: FakeSocket[] = [];
    const stream = new EventStream({
      getAfterSeq: () => 0,
      getToken: () => 'bad',
      baseUrl: () => 'ws://x/ws',
      onEvents: () => undefined,
      onStatus: () => undefined,
      onAuthFailure,
      createSocket: (u) => {
        const s = new FakeSocket(u);
        sockets.push(s);
        return s;
      },
    });
    stream.start();
    sockets[0].onclose?.({ code: 4401 });
    expect(onAuthFailure).toHaveBeenCalledOnce();
  });

  it('backoff is capped', () => {
    expect(backoffDelay(0)).toBe(500);
    expect(backoffDelay(3)).toBe(4000);
    expect(backoffDelay(20)).toBe(15000);
  });
});
