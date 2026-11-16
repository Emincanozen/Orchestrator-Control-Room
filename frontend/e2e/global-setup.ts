import { BASE_URL, PORT, startBackend, token } from './support/backend';
import { prepareSkillSources } from './support/skills';

/**
 * Starts the real backend (mock provider only) on a temp data dir, then allows this port's
 * Origin for the WebSocket: policies.yaml only lists 8765/5173, so a dashboard served from any
 * other port would be rejected with close code 4403 (see the e2e report).
 */
export default async function globalSetup(): Promise<void> {
  await startBackend(prepareSkillSources);
  const origins = [`http://127.0.0.1:${PORT}`, `http://localhost:${PORT}`];
  const res = await fetch(`${BASE_URL}/api/settings`, {
    method: 'PUT',
    headers: { Authorization: `Bearer ${token()}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ values: { security: { allowed_origins: origins } } }),
  });
  if (!res.ok) throw new Error(`could not allow the e2e origin: ${res.status} ${await res.text()}`);
  // zeta/zulu are extra fake providers that only the leadership switch tests use. The default leader may have been
  // recorded as one of them at first start: move it to mock, then keep them out of routing (and out of the leader
  // candidates) until a test opts in.
  const auth = { Authorization: `Bearer ${token()}`, 'Content-Type': 'application/json' };
  const sw = await fetch(`${BASE_URL}/api/leadership/switch`, { method: 'POST', headers: auth, body: JSON.stringify({ provider: 'mock', model: 'mock-model' }) });
  if (!sw.ok && sw.status !== 422) throw new Error(`could not pin the e2e leader to mock: ${sw.status} ${await sw.text()}`);
  const off = await fetch(`${BASE_URL}/api/routing/preferences`, { method: 'PUT', headers: auth, body: JSON.stringify({ disabled_providers: ['zeta', 'zulu'] }) });
  if (!off.ok) throw new Error(`could not disable the extra e2e providers: ${off.status} ${await off.text()}`);
  process.env.E2E_BASE_URL = BASE_URL;
}
