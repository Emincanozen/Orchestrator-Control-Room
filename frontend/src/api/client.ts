import { clearToken, getToken } from './auth';
import type { ErrorBody } from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
  get unauthorized(): boolean {
    return this.status === 401;
  }
  /** Endpoint not implemented by the backend yet (404/501): UI shows "not available yet". */
  get unavailable(): boolean {
    return this.status === 404 || this.status === 501;
  }
}

export function isUnavailable(err: unknown): boolean {
  return err instanceof ApiError && err.unavailable;
}

export const API_BASE = '/api';

type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
}

export function buildUrl(path: string, query?: Query): string {
  const qs = new URLSearchParams();
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
    }
  }
  const s = qs.toString();
  return `${API_BASE}${path}${s ? `?${s}` : ''}`;
}

/** Parses the section 6 error envelope; tolerates non-JSON bodies (proxies, 502s). */
export async function parseError(res: Response): Promise<ApiError> {
  let code = `http_${res.status}`;
  let message = res.statusText || `HTTP ${res.status}`;
  let details: Record<string, unknown> = {};
  try {
    const text = await res.text();
    if (text) {
      const data = JSON.parse(text) as { error?: Partial<ErrorBody>; detail?: unknown };
      if (data && typeof data === 'object' && data.error && typeof data.error === 'object') {
        code = data.error.code ?? code;
        message = data.error.message ?? message;
        details = data.error.details ?? {};
      } else if (typeof data?.detail === 'string') {
        message = data.detail;
      }
    }
  } catch {
    /* non-JSON body: keep defaults */
  }
  return new ApiError(res.status, code, message, details);
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), { method: opts.method ?? 'GET', headers, body, signal: opts.signal });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new ApiError(0, 'network', 'Cannot reach the orchestrator backend (is it running?)');
  }
  if (!res.ok) {
    const err = await parseError(res);
    if (err.unauthorized) {
      clearToken();
      onUnauthorized?.();
    }
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** List endpoints return bare JSON arrays; anything else (e.g. an unexpected body) yields an empty list. */
export function unwrapList<T>(data: unknown): T[] {
  return Array.isArray(data) ? (data as T[]) : [];
}
