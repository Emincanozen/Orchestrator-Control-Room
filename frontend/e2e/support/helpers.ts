import { execFileSync } from 'node:child_process';
import { mkdirSync, realpathSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { BASE_URL, token, workDir } from './backend';

let counter = 0;
/** Unique, human-readable suffix so tests never collide on titles. */
export const uid = (prefix: string): string => `${prefix}-${Date.now().toString(36)}${(counter++).toString(36)}`;

export async function api<T = unknown>(method: string, route: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}/api${route}`, {
    method,
    headers: { Authorization: `Bearer ${token()}`, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${route} -> ${res.status} ${await res.text()}`);
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, stdio: 'ignore' });

/** A real git repository with one commit, inside the temp e2e dir. */
export function makeGitRepo(name: string): string {
  const dir = path.join(workDir(), name);
  mkdirSync(dir, { recursive: true });
  git(dir, 'init', '--quiet', '-b', 'main');
  git(dir, 'config', 'user.name', 'E2E');
  git(dir, 'config', 'user.email', 'e2e@example.invalid');
  git(dir, 'config', 'commit.gpgsign', 'false');
  writeFileSync(path.join(dir, 'README.md'), `# ${name}\n`);
  git(dir, 'add', '-A');
  git(dir, 'commit', '--quiet', '-m', 'initial');
  return realpathSync(dir);
}

export function makePlainDir(name: string): string {
  const dir = path.join(workDir(), name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, 'notes.txt'), 'not a git repo\n');
  return realpathSync(dir);
}

export interface ApiTask {
  id: string;
  title: string;
  state: string;
  worktree_path: string | null;
  attempt_count: number;
  active_seconds: number;
}
export interface ApiProject {
  id: string;
  name: string;
  path: string;
  is_git: boolean;
}

export async function createProjectApi(name: string, dir = makeGitRepo(name)): Promise<ApiProject> {
  return api<ApiProject>('POST', '/projects', { name, path: dir });
}

export async function createTaskApi(projectId: string, title: string, o: Record<string, unknown> = {}): Promise<ApiTask> {
  return api<ApiTask>('POST', '/tasks', { project_id: projectId, title, preferred_provider: 'mock', ...o });
}

/** Polls the REST API (not the UI) for a task state. */
export async function taskState(id: string): Promise<string> {
  return (await api<ApiTask>('GET', `/tasks/${id}`)).state;
}

/** Opens the dashboard the way a user does: with #token= in the address (the form the CLI opens). */
export async function openDashboard(page: Page, hash = '#/mission'): Promise<void> {
  await page.goto(`/#token=${encodeURIComponent(token())}`);
  if (hash) await page.evaluate((h) => { window.location.hash = h; }, hash);
  await expect(page.getByTestId('conn-status')).toContainText('live');
}

export const column = (page: Page, state: string): Locator => page.getByTestId(`col-${state}`);
export const taskCard = (page: Page, title: string): Locator => page.getByRole('button', { name: `Open task ${title}` });

/** Asserts that the board shows the task in the given state column. */
export async function expectOnBoard(page: Page, title: string, state: string, timeout = 30_000): Promise<void> {
  await expect(column(page, state).getByRole('button', { name: `Open task ${title}` })).toBeVisible({ timeout });
}

export async function openTask(page: Page, title: string): Promise<void> {
  await taskCard(page, title).click();
  await expect(page.getByRole('dialog')).toBeVisible();
}

export async function closeDrawer(page: Page): Promise<void> {
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toBeHidden();
}

export const tab = (page: Page, name: string): Locator => page.getByRole('tab', { name, exact: true });
