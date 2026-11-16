import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** Lifecycle of the backend under test. State lives in a JSON file so every process (global setup, workers, teardown) sees it. */

export const PORT = Number(process.env.E2E_PORT ?? 8811);
export const BASE_URL = `http://127.0.0.1:${PORT}`;
const REPO_ROOT = path.resolve(HERE, '..', '..', '..');
const BACKEND_DIR = path.join(REPO_ROOT, 'backend');
const SERVER = path.join(HERE, 'mock_server.py');
const STATE_FILE = path.join(tmpdir(), `orchestrator-e2e-${PORT}.json`);

interface State {
  root: string;
  dataDir: string;
  pid: number | null;
}

function readState(): State {
  return JSON.parse(readFileSync(STATE_FILE, 'utf8')) as State;
}
function writeState(s: State): void {
  writeFileSync(STATE_FILE, JSON.stringify(s));
}

export function dataDir(): string {
  return readState().dataDir;
}
export function workDir(): string {
  return path.join(readState().root, 'repos');
}
export function skillsConfigPath(): string {
  return path.join(readState().root, 'skills-config.json');
}
export function skillRepoDir(): string {
  return path.join(readState().root, 'skill-repos');
}
export function token(): string {
  return readFileSync(path.join(dataDir(), 'api-token'), 'ascii').trim();
}

function pythonCommand(): { cmd: string; args: string[] } {
  const venv = path.join(BACKEND_DIR, '.venv', 'Scripts', 'python.exe');
  if (existsSync(venv)) return { cmd: venv, args: [SERVER] };
  return { cmd: 'uv', args: ['run', '--project', BACKEND_DIR, 'python', SERVER] };
}

export function killTree(pid: number): void {
  try {
    if (process.platform === 'win32') execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    else process.kill(-pid, 'SIGKILL');
  } catch {
    /* already gone */
  }
}

async function waitHealthy(timeoutMs: number): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      const r = await fetch(`${BASE_URL}/api/health`);
      if (r.ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`backend did not become healthy on ${BASE_URL} within ${timeoutMs} ms`);
}

async function waitDown(timeoutMs: number): Promise<void> {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try {
      await fetch(`${BASE_URL}/api/health`);
    } catch {
      return;
    }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error('backend still answering after kill');
}

function launch(s: State): number {
  const { cmd, args } = pythonCommand();
  const log = openSync(path.join(s.root, 'backend.log'), 'a');
  const skillsArgs = existsSync(path.join(s.root, 'skills-config.json')) ? ['--skills-config', path.join(s.root, 'skills-config.json')] : [];
  const child = spawn(cmd, [...args, '--port', String(PORT), '--data-dir', s.dataDir, ...skillsArgs], {
    cwd: BACKEND_DIR,
    stdio: ['ignore', log, log],
    detached: true,
    windowsHide: true,
    env: { ...process.env, ORCHESTRATOR_ENABLE_MOCK: '1', ORCHESTRATOR_DISABLE_REAL_PROVIDERS: '1', ORCHESTRATOR_DATA_DIR: s.dataDir, PYTHONUNBUFFERED: '1' },
  });
  child.unref();
  if (!child.pid) throw new Error('failed to spawn the backend');
  return child.pid;
}

/** Creates the temp data dir and starts the backend. Called once from global setup. */
export async function startBackend(prepare?: (root: string) => void): Promise<void> {
  if (existsSync(STATE_FILE)) {
    // A previous run was killed before teardown: clean it up first.
    await stopBackend();
  }
  const root = path.join(tmpdir(), `orchestrator-e2e-${PORT}-${Date.now()}`);
  mkdirSync(path.join(root, 'data'), { recursive: true });
  mkdirSync(path.join(root, 'repos'), { recursive: true });
  const s: State = { root, dataDir: path.join(root, 'data'), pid: null };
  writeState(s);
  prepare?.(root);
  s.pid = launch(s);
  writeState(s);
  await waitHealthy(60_000);
}

/** Kills the backend and starts a new process on the SAME data dir (and port). */
export async function restartBackend(downMs = 0): Promise<void> {
  const s = readState();
  if (s.pid) killTree(s.pid);
  s.pid = null;
  writeState(s);
  await waitDown(15_000);
  if (downMs) await new Promise((r) => setTimeout(r, downMs));
  s.pid = launch(s);
  writeState(s);
  await waitHealthy(60_000);
}

/** Stops the backend and deletes every temp file. Idempotent. */
export async function stopBackend(): Promise<void> {
  if (!existsSync(STATE_FILE)) return;
  const s = readState();
  if (s.pid) killTree(s.pid);
  await new Promise((r) => setTimeout(r, 500));
  try {
    rmSync(s.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 });
  } catch {
    /* best effort: temp dir */
  }
  rmSync(STATE_FILE, { force: true });
}
