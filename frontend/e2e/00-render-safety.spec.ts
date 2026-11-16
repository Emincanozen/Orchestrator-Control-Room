import { expect, test } from '@playwright/test';
import { api, createProjectApi, createTaskApi, openDashboard, taskState, uid } from './support/helpers';

const callCount = async () => (await api<{ total: number }>('GET', '/_e2e/provider-calls')).total;

test('all eleven screens render without initiating a provider call', async ({ page }) => {
  // This spec runs first against the fresh backend. Count actual mock execute
  // calls, including leader/advisor calls, rather than merely blocking paid APIs.
  expect(await api('GET', '/tasks')).toEqual([]);
  const before = await callCount();
  await openDashboard(page);
  const screens = ['Office', 'Mission Control', 'Approvals', 'Leadership', 'Providers & Usage',
    'Routing', 'Analytics', 'Knowledge', 'Skills & MCP', 'Logs', 'Settings'];
  for (const name of screens) {
    await page.getByRole('link', { name, exact: true }).click();
    await expect(page.getByRole('heading', { level: 1, name, exact: true })).toBeVisible();
    expect(await callCount(), name).toBe(before);
  }
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Settings', exact: true })).toBeVisible();
  // Observe several scheduler ticks for delayed work, not just the first render.
  await page.waitForTimeout(1000);
  expect(await callCount()).toBe(before);
  expect(await api('GET', '/tasks')).toEqual([]);

  // Positive control: the counter must detect an explicitly requested task.
  const project = await createProjectApi(uid('render-control'));
  const task = await createTaskApi(project.id, uid('counter-control'), {
    verify_commands: [{ file_exists: 'result.txt' }], autostart: true,
  });
  await expect.poll(() => taskState(task.id), { timeout: 30_000 }).toBe('COMPLETED');
  expect(await callCount()).toBeGreaterThan(before);
});
