const KEY = 'orchestrator.token';

type Listener = () => void;
const listeners = new Set<Listener>();

let memoryToken: string | null = null;

export function getToken(): string | null {
  try {
    const t = sessionStorage.getItem(KEY);
    if (t) return t;
  } catch {
    /* sessionStorage unavailable */
  }
  return memoryToken;
}

export function setToken(token: string): void {
  memoryToken = token;
  try {
    sessionStorage.setItem(KEY, token);
  } catch {
    /* keep in memory only */
  }
  listeners.forEach((l) => l());
}

export function clearToken(): void {
  memoryToken = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}

export function onAuthChange(l: Listener): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/**
 * Called once at startup: picks the token off the URL, stores it in sessionStorage and strips it from
 * the address bar (replacing the history entry). The launcher uses the fragment form `/#token=<token>`
 * (a fragment is never sent to the server or written to access logs); the old `?token=` query is still accepted.
 */
export function initTokenFromUrl(loc: Location = window.location, hist: History = window.history): void {
  const url = new URL(loc.href);
  const frag = new URLSearchParams(url.hash.startsWith('#') ? url.hash.slice(1) : url.hash);
  const fromHash = frag.get('token');
  const fromQuery = url.searchParams.get('token');
  const token = fromHash || fromQuery;
  if (!token) return;
  setToken(token);
  if (fromQuery !== null) url.searchParams.delete('token');
  let hash = url.hash;
  if (fromHash !== null) {
    frag.delete('token');
    const rest = frag.toString();
    hash = rest ? `#${rest}` : '';
  }
  hist.replaceState(hist.state, '', url.pathname + url.search + hash);
}
