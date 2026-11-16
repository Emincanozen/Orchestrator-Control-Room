import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { clearToken, getToken, initTokenFromUrl, setToken } from './auth';
import { ApiError, buildUrl, request, setUnauthorizedHandler, unwrapList } from './client';
import { api } from './endpoints';

async function fail(p: Promise<unknown>): Promise<ApiError> {
  try {
    await p;
  } catch (e) {
    return e as ApiError;
  }
  throw new Error('expected rejection');
}
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('api client', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    clearToken();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setUnauthorizedHandler(null);
  });

  it('sends the bearer token and JSON body', async () => {
    setToken('abc');
    fetchMock.mockResolvedValue(json(200, { id: 'prj_1' }));
    await request('/projects', { method: 'POST', body: { name: 'x' } });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects');
    expect(init.headers.Authorization).toBe('Bearer abc');
    expect(init.body).toBe('{"name":"x"}');
  });

  it('parses the section 6 error envelope', async () => {
    fetchMock.mockResolvedValue(json(422, { error: { code: 'invalid_request', message: 'bad path', details: { field: 'path' } } }));
    const err = await fail(request('/projects'));
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.code).toBe('invalid_request');
    expect(err.message).toBe('bad path');
    expect(err.details).toEqual({ field: 'path' });
  });

  it('survives non-JSON error bodies', async () => {
    fetchMock.mockResolvedValue(new Response('<html>Bad gateway</html>', { status: 502, statusText: 'Bad Gateway' }));
    const err = await fail(request('/status'));
    expect(err.code).toBe('http_502');
    expect(err.message).toBe('Bad Gateway');
  });

  it('flags 404/501 as unavailable', async () => {
    fetchMock.mockResolvedValue(json(404, { error: { code: 'not_found', message: 'nope' } }));
    expect((await fail(api.analyticsSummary())).unavailable).toBe(true);
    fetchMock.mockResolvedValue(json(501, { error: { code: 'not_implemented', message: 'later' } }));
    expect((await fail(api.skills())).unavailable).toBe(true);
    fetchMock.mockResolvedValue(json(500, { error: { code: 'internal', message: 'boom' } }));
    expect((await fail(api.skills())).unavailable).toBe(false);
  });

  it('clears the token and notifies on 401', async () => {
    setToken('stale');
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetchMock.mockResolvedValue(json(401, { error: { code: 'unauthorized', message: 'no' } }));
    const err = await fail(request('/status'));
    expect(err.unauthorized).toBe(true);
    expect(getToken()).toBeNull();
    expect(handler).toHaveBeenCalledOnce();
  });

  it('maps network failure to a clear error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const err = await fail(request('/status'));
    expect(err.code).toBe('network');
  });

  it('handles 204 and builds query strings without empty values', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.deleteProject('prj_1')).resolves.toBeUndefined();
    expect(buildUrl('/tasks', { project_id: 'a', state: '', x: undefined })).toBe('/api/tasks?project_id=a');
  });

  it('unwrapList accepts bare arrays only', () => {
    expect(unwrapList([1])).toEqual([1]);
    expect(unwrapList({ items: [2] })).toEqual([]);
    expect(unwrapList({})).toEqual([]);
  });
});

describe('token bootstrap', () => {
  beforeEach(() => clearToken());
  it('stores ?token= in sessionStorage and strips it from the URL', () => {
    const replaceState = vi.fn();
    initTokenFromUrl({ href: 'http://127.0.0.1:8765/?token=SECRET&x=1#/mission' } as Location, { state: null, replaceState } as unknown as History);
    expect(getToken()).toBe('SECRET');
    expect(sessionStorage.getItem('orchestrator.token')).toBe('SECRET');
    expect(replaceState).toHaveBeenCalledWith(null, '', '/?x=1#/mission');
  });
  it('reads the token from the URL fragment and strips only it', () => {
    const replaceState = vi.fn();
    initTokenFromUrl({ href: 'http://127.0.0.1:8765/#token=FRAG' } as Location, { state: null, replaceState } as unknown as History);
    expect(getToken()).toBe('FRAG');
    expect(replaceState).toHaveBeenCalledWith(null, '', '/');
  });
  it('keeps a query string and other fragment params when stripping the fragment token', () => {
    const replaceState = vi.fn();
    initTokenFromUrl({ href: 'http://127.0.0.1:8765/?x=1#token=FRAG&tab=2' } as Location, { state: null, replaceState } as unknown as History);
    expect(getToken()).toBe('FRAG');
    expect(replaceState).toHaveBeenCalledWith(null, '', '/?x=1#tab=2');
  });
  it('does not touch a route fragment that carries no token', () => {
    const replaceState = vi.fn();
    initTokenFromUrl({ href: 'http://127.0.0.1:8765/#/mission' } as Location, { state: null, replaceState } as unknown as History);
    expect(getToken()).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
  it('leaves things alone when there is no token', () => {
    const replaceState = vi.fn();
    initTokenFromUrl({ href: 'http://127.0.0.1:8765/' } as Location, { state: null, replaceState } as unknown as History);
    expect(getToken()).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
