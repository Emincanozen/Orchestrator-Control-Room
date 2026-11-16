import { expect, test } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { api, closeDrawer, createProjectApi, createTaskApi, expectOnBoard, makeGitRepo, makePlainDir, openDashboard, openTask, tab, uid } from './support/helpers';

// Documentation screenshots of the real dashboard: `SCREENSHOTS=1 npx playwright test screenshots`.
const OUT = path.resolve(import.meta.dirname ?? '.', '..', '..', 'docs', 'screenshots');
test.skip(!process.env.SCREENSHOTS, 'set SCREENSHOTS=1 to regenerate docs/screenshots');
test.use({ viewport: { width: 1920, height: 1000 } });

const PASS = `node -e "process.exit(require('fs').existsSync('result.txt')?0:1)"`;

test('capture the dashboard', async ({ page }) => {
  const project = await createProjectApi(uid('demo-app'));
  const done = uid('Add parser');
  const failed = uid('Fix flaky build');
  const hang = uid('Refactor router');
  const slow = uid('Write release notes');
  const unverifiable = uid('Tidy config');
  await createTaskApi(project.id, done, { verify_commands: [PASS], autostart: true });
  await createTaskApi(project.id, failed, { verify_commands: ['node -e process.exit(1)'], autostart: true });
  await createTaskApi(project.id, hang, { description: 'MOCK:HANG', verify_commands: [PASS], autostart: true });
  await createTaskApi(project.id, unverifiable, { autostart: true });
  await createProjectApi(uid('docs-site'), makePlainDir(uid('docs-site-dir')));
  await openDashboard(page);
  await expectOnBoard(page, done, 'COMPLETED');
  await expectOnBoard(page, failed, 'FAILED');
  await expectOnBoard(page, hang, 'PLANNING');
  await expectOnBoard(page, unverifiable, 'NEEDS_APPROVAL');
  await page.getByLabel('Hide empty columns').check();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, 'dashboard-mission-control.png') });

  // live log of a task while it runs
  await createTaskApi(project.id, slow, { description: 'MOCK:SLOW', verify_commands: [PASS], autostart: true });
  await expectOnBoard(page, slow, 'RUNNING');
  await openTask(page, slow);
  await tab(page, 'Live log').click();
  await page.getByRole('log', { name: 'Live task output' }).getByText('slow task: writing result.txt').waitFor();
  await page.screenshot({ path: path.join(OUT, 'dashboard-task-live-log.png') });
  await expect.poll(async () => (await api<{ title: string; state: string }[]>('GET', '/tasks')).find((x) => x.title === slow)?.state, { timeout: 30_000 }).toBe('COMPLETED');
  await tab(page, 'Diff').click();
  await page.getByTestId('diff').waitFor();
  await page.screenshot({ path: path.join(OUT, 'dashboard-task-diff.png') });
  await closeDrawer(page);

  await page.getByRole('link', { name: 'Approvals', exact: true }).click();
  await page.getByRole('heading', { name: 'Approvals' }).waitFor();
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, 'dashboard-approvals.png') });
});

test('capture providers, routing and skills', async ({ page }) => {
  const project = await createProjectApi(uid('demo-app'));
  const title = uid('Add parser');
  const task = await createTaskApi(project.id, title, { verify_commands: [PASS], autostart: true });
  await openDashboard(page, '#/mission');
  await expectOnBoard(page, title, 'COMPLETED');

  await page.goto('/#/providers');
  await page.getByTestId('provider-mock').waitFor();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, 'dashboard-providers.png') });

  await page.goto(`/#/routing?task=${task.id}`);
  await page.getByLabel('Title', { exact: true }).fill('Implement a login page');
  await page.getByRole('button', { name: /Preview/ }).click();
  await page.getByRole('region', { name: 'Preview result' }).waitFor();
  const row = page.getByTestId('decision').first();
  await row.getByRole('button').click();
  await row.getByTestId('explanation').waitFor();
  await row.scrollIntoViewIfNeeded();
  await page.getByRole('heading', { name: 'Why this model?' }).scrollIntoViewIfNeeded();
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, 'dashboard-routing.png') });

  await page.goto('/#/skills');
  await page.getByRole('button', { name: 'Open skill e2e-alpha' }).click();
  const d = page.getByTestId('skill-detail');
  await d.getByRole('button', { name: /Download/ }).click();
  await page.getByRole('dialog').last().getByRole('button', { name: 'Download', exact: true }).click();
  await page.locator('[data-skill-state="DOWNLOADED_AND_DISABLED"]').first().waitFor();
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, 'dashboard-skills.png') });
});

test('capture leadership, knowledge and analytics', async ({ page }) => {
  const name = uid('shop-api');
  const dir = makeGitRepo(name);
  mkdirSync(path.join(dir, 'docs'), { recursive: true });
  writeFileSync(path.join(dir, 'docs', 'architecture.md'), '# Architecture\n\nThe order service keeps an append-only ledger and replays it after a crash.\n');
  writeFileSync(path.join(dir, 'docs', 'runbook.md'), '# Runbook\n\nRestart the worker, then check the ledger replay finished.\n');
  const project = await createProjectApi(name, dir);
  for (const [t, o] of [
    ['Add ledger replay', { verify_commands: [{ file_exists: 'result.txt' }] }],
    ['Fix flaky retry test', { description: 'MOCK:FIXLATER', verify_commands: [{ file_exists: 'fix.txt' }] }],
    ['Migrate legacy config', { verify_commands: ['node -e "process.exit(1)"'] }],
    ['Write release notes', { verify_commands: [{ file_exists: 'result.txt' }] }],
  ] as const) {
    await createTaskApi(project.id, uid(t), { ...o, autostart: true });
  }
  await expect
    .poll(async () => (await api<{ state: string }[]>('GET', `/tasks?project_id=${project.id}`)).filter((x) => ['COMPLETED', 'FAILED'].includes(x.state)).length, { timeout: 60_000 })
    .toBe(4);
  await api('POST', `/projects/${project.id}/objectives`, { text: 'Add CSV export to the orders report, with tests', wait: true });
  await api('POST', '/advisor/consult', { problem: 'Should the ledger replay run at startup or lazily on first request?', options: ['at startup', 'lazily on first request'], constraints: ['startup under 2 s'] });
  await api('POST', '/knowledge/index', { project_id: project.id, sources: ['docs'], wait: true });

  await page.setViewportSize({ width: 1920, height: 1750 });
  await openDashboard(page, '#/leadership');
  await expect(page.getByTestId('current-leader')).toBeVisible();
  await page.getByLabel('Problem').fill('Should the ledger replay run at startup or lazily?');
  await page.getByRole('button', { name: 'Ask advisor' }).click();
  await expect(page.getByTestId('consult-answer')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('decision-row').first().click();
  await page.waitForTimeout(500);
  await page.evaluate(() => document.querySelectorAll('*').forEach((e) => ((e as HTMLElement).scrollTop = 0)));
  await page.screenshot({ path: path.join(OUT, 'dashboard-leadership.png'), fullPage: false });

  await page.setViewportSize({ width: 1920, height: 1500 });
  await page.goto('/#/knowledge');
  await page.getByRole('textbox', { name: 'Search knowledge' }).fill('ledger');
  await page.getByRole('button', { name: /^Search$/ }).click();
  await expect(page.getByTestId('hit').first()).toBeVisible();
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(OUT, 'dashboard-knowledge.png'), fullPage: false });

  await page.setViewportSize({ width: 1920, height: 2050 });
  await page.goto('/#/analytics');
  await page.getByLabel('Project', { exact: true }).selectOption({ label: name });
  await expect(page.getByTestId('kpi-tasks_completed').getByTestId('kpi-n')).toHaveText('n = 7');
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT, 'dashboard-analytics.png'), fullPage: false });
});
