import { expect, test } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { api, createProjectApi, createTaskApi, makeGitRepo, makePlainDir, openDashboard, uid } from './support/helpers';

const PASS_VERIFY = { file_exists: 'result.txt' };
const FAIL_VERIFY = `node -e "process.exit(1)"`;

interface ApiVerification {
  status: string;
}
const verifications = (taskId: string) => api<ApiVerification[]>('GET', `/tasks/${taskId}/verification`);

/** zeta/zulu (see support/mock_server.py) are kept out of routing so they cannot take tasks; switch tests opt in. */
const extraProviders = (allowed: boolean) => api('PUT', '/routing/preferences', { disabled_providers: allowed ? [] : ['zeta', 'zulu'] });
const ensureMockLeader = async () => {
  const cur = (await api<{ leader: { provider: string } }>('GET', '/leadership')).leader.provider;
  if (cur !== 'mock') await api('POST', '/leadership/switch', { provider: 'mock', model: 'mock-model' });
};
test.beforeAll(async () => {
  await extraProviders(false);
});

test.describe('analytics', () => {
  test('shows measured values with n matching what happened, and "No data yet" where nothing exists', async ({ page }) => {
    const name = uid('analytics');
    const project = await createProjectApi(name);
    const passing = await createTaskApi(project.id, uid('passes'), { verify_commands: [PASS_VERIFY], autostart: true });
    const fixed = await createTaskApi(project.id, uid('fixed-later'), { description: 'MOCK:FIXLATER', verify_commands: [{ file_exists: 'fix.txt' }], autostart: true });
    const failing = await createTaskApi(project.id, uid('fails'), { verify_commands: [FAIL_VERIFY], autostart: true });
    await openDashboard(page, '#/mission');
    await expect.poll(async () => (await api<{ state: string }>('GET', `/tasks/${passing.id}`)).state, { timeout: 45_000 }).toBe('COMPLETED');
    await expect.poll(async () => (await api<{ state: string }>('GET', `/tasks/${fixed.id}`)).state, { timeout: 45_000 }).toBe('COMPLETED');
    await expect.poll(async () => (await api<{ state: string }>('GET', `/tasks/${failing.id}`)).state, { timeout: 45_000 }).toBe('FAILED');

    // ground truth from independent endpoints
    const all = [...(await verifications(passing.id)), ...(await verifications(fixed.id)), ...(await verifications(failing.id))];
    const passed = all.filter((v) => v.status === 'passed').length;
    expect(passed).toBe(2);
    expect(all.length).toBeGreaterThan(passed); // at least one failing verification happened (fixed-later + failing)

    await page.goto(page.url().split('#')[0] + '#/analytics');
    await page.getByLabel('Project', { exact: true }).selectOption({ label: name });

    const tile = (id: string) => page.getByTestId(`kpi-${id}`);
    await expect(tile('tasks_completed').getByTestId('kpi-value')).toHaveText('2');
    await expect(tile('tasks_completed').getByTestId('kpi-n')).toHaveText('n = 3');
    await expect(tile('tasks_completed')).toHaveAttribute('data-exactness', 'measured');
    await expect(tile('verified_completion_rate').getByTestId('kpi-value')).toHaveText('66.7%');
    await expect(tile('verified_completion_rate').getByTestId('kpi-n')).toHaveText('n = 3');
    await expect(tile('verified_completion_rate').getByTestId('low-evidence')).toBeVisible(); // 3 < 5
    await expect(tile('test_pass_rate').getByTestId('kpi-n')).toHaveText(`n = ${all.length}`);
    const rate = (passed / all.length) * 100;
    await expect(tile('test_pass_rate').getByTestId('kpi-value')).toHaveText(`${Number(rate.toFixed(1))}%`);
    await expect(tile('repair_cycles_total').getByTestId('kpi-value')).not.toHaveText('No data yet');

    // nothing exists for these in the mock setup: never rendered as 0
    // prompt sizes are measured from the runs above (prompt / handoff / knowledge rows with p50, p90, mean, max)
    await expect(tile('handoff_prompt_chars').getByTestId('kpi-value')).toContainText('p50');
    await expect(tile('handoff_prompt_chars').getByTestId('kpi-value')).toContainText('prompt');
    await expect(tile('handoff_prompt_chars')).not.toHaveAttribute('data-exactness', 'missing');
    for (const id of ['cost_usd_estimated', 'handoff_checkpoints']) {
      await expect(tile(id).getByTestId('kpi-value')).toHaveText('No data yet');
      await expect(tile(id)).toHaveAttribute('data-exactness', 'missing');
    }

    // charts
    await expect(page.getByTestId('chart-failures')).toBeVisible();
    await expect(page.getByTestId('chart-tokens')).toContainText('mock/mock-model');
    await expect(page.getByTestId('timeseries-chart')).toBeVisible();

    // model table: the mock model with its verified runs as n
    const row = page.getByTestId('model-row').filter({ hasText: 'mock-model' }).first();
    await expect(row.getByTestId('success-rate')).toContainText('n = ');
    await expect(page.getByRole('table', { name: 'Agent history' })).toBeVisible();

    // a project with no tasks: everything reads "No data yet", not zero
    const empty = uid('empty-analytics');
    await createProjectApi(empty, makePlainDir(uid('empty-dir')));
    await page.reload();
    await page.getByLabel('Project', { exact: true }).selectOption({ label: empty });
    await expect(tile('tasks_completed').getByTestId('kpi-value')).toHaveText('No data yet');
    await expect(tile('verified_completion_rate').getByTestId('kpi-value')).toHaveText('No data yet');
    await expect(page.getByTestId('chart-failures').getByTestId('no-data')).toBeVisible();
  });

  test('adaptive settings validate bounds and persist', async ({ page }) => {
    await openDashboard(page, '#/analytics');
    const min = page.getByLabel('Minimum verified runs');
    await min.fill('2');
    await expect(page.getByText('Whole number from 3 to 500.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save adaptive settings' })).toBeDisabled();
    await min.fill('25');
    await page.getByRole('button', { name: 'Save adaptive settings' }).click();
    await expect.poll(async () => (await api<{ min_samples: number }>('GET', '/analytics/adaptive-settings')).min_samples).toBe(25);
    await api('PUT', '/analytics/adaptive-settings', { min_samples: 20 });
  });
});

test.describe('knowledge', () => {
  test('index a temp project, find a phrase with provenance, open it and delete it', async ({ page }) => {
    const name = uid('know');
    const dir = makeGitRepo(name);
    mkdirSync(path.join(dir, 'docs'), { recursive: true });
    const phrase = `zebraquartz${Date.now().toString(36)}`;
    writeFileSync(path.join(dir, 'docs', 'guide.md'), `# Guide\n\nThe ${phrase} protocol keeps caches warm. <script>window.__x=1</script>\n`);
    await createProjectApi(name, dir);

    await openDashboard(page, '#/knowledge');
    await expect(page.getByTestId('lexical-note')).toContainText('full-text (lexical');
    await page.getByRole('button', { name: /Index now/ }).click();
    const dlg = page.getByRole('dialog');
    await dlg.getByLabel('Project', { exact: true }).selectOption({ label: name });
    await dlg.getByRole('button', { name: 'Start indexing' }).click();
    const report = dlg.getByTestId('index-report');
    await expect(report).toBeVisible({ timeout: 30_000 });
    expect(Number(await report.getByTestId('report-added').innerText())).toBeGreaterThan(0);
    await dlg.getByRole('button', { name: 'Close' }).first().click();

    await page.getByRole('textbox', { name: 'Search knowledge' }).fill(phrase);
    await page.getByRole('button', { name: /^Search$/ }).click();
    const hit = page.getByTestId('hit').first();
    await expect(hit).toBeVisible();
    await expect(hit.locator('mark')).toContainText(phrase);
    await expect(hit.getByTestId('provenance')).toContainText('guide.md');
    await expect(hit.getByTestId('provenance')).toContainText(`project: ${name}`);
    await expect(page.getByTestId('used-vectors')).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { __x?: number }).__x)).toBeUndefined();

    await hit.getByRole('button').first().click();
    await expect(page.getByTestId('doc-body')).toContainText(`The ${phrase} protocol`);
    await page.getByRole('button', { name: 'Delete from index' }).click();
    await page.getByRole('dialog').last().getByRole('button', { name: 'Delete', exact: true }).click();
    await expect(page.getByTestId('doc-body')).toBeHidden();

    await page.getByRole('textbox', { name: 'Search knowledge' }).fill(phrase);
    await page.getByRole('button', { name: /^Search$/ }).click();
    await expect(page.getByText('No matches')).toBeVisible();
  });
});

test.describe('logs', () => {
  test('shows the events of a run live, filters them and jumps to the task', async ({ page }) => {
    const project = await createProjectApi(uid('logsproj'));
    await openDashboard(page, '#/logs');
    await expect(page.getByTestId('event-log').or(page.getByText('No events received yet')).first()).toBeVisible();
    const title = uid('logged');
    const task = await createTaskApi(project.id, title, { description: 'MOCK:SLOW', verify_commands: [PASS_VERIFY], autostart: true });

    await page.getByLabel('Task id').fill(task.id);
    const rows = page.getByTestId('log-row');
    await expect(rows.filter({ hasText: 'task.state_changed' }).first()).toBeVisible({ timeout: 30_000 });
    await expect(rows.filter({ hasText: 'run.output' }).first()).toBeVisible({ timeout: 30_000 });
    await page.getByLabel('Event type prefix').fill('run.');
    await expect(rows.first()).toContainText('run.');
    for (const r of await rows.all()) await expect(r).toContainText('run.');

    await rows.first().click();
    await expect(page.getByTestId('payload')).toContainText('{');
    await page.getByTestId('jump-task').click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog')).toContainText(title);
  });

  test('pause freezes the list and resume merges new events', async ({ page }) => {
    const project = await createProjectApi(uid('pauseproj'));
    await openDashboard(page, '#/logs');
    await page.getByTestId('pause-toggle').click();
    const before = await page.getByTestId('event-log').getAttribute('data-rows').catch(() => '0');
    await createTaskApi(project.id, uid('during-pause'), { verify_commands: [PASS_VERIFY], autostart: true });
    await expect(page.getByTestId('pause-toggle')).toContainText(/Resume \([1-9]/, { timeout: 20_000 });
    expect(await page.getByTestId('event-log').getAttribute('data-rows').catch(() => '0')).toBe(before);
    await page.getByTestId('pause-toggle').click();
    await expect.poll(async () => Number(await page.getByTestId('event-log').getAttribute('data-rows'))).toBeGreaterThan(Number(before));
  });
});

test.describe('leadership', () => {
  test.beforeAll(async () => {
    await ensureMockLeader();
    await extraProviders(true);
  });
  test.afterAll(async () => {
    await ensureMockLeader();
    await extraProviders(false);
  });

  test('shows the leader, disabled ineligible candidates with reasons, and updates the top bar', async ({ page }) => {
    const lead = await api<{ leader: { provider: string; model: string }; candidates: { leader: { provider: string; model: string; eligible: boolean; reasons: string[] }[] } }>('GET', '/leadership');
    await openDashboard(page, '#/leadership');
    await expect(page.getByTestId('current-leader')).toContainText(`${lead.leader.provider} / ${lead.leader.model}`);
    await expect(page.getByTestId('leader')).toContainText(lead.leader.provider);
    const ineligible = lead.candidates.leader.find((c) => !c.eligible);
    if (ineligible) {
      const opt = page.getByRole('combobox', { name: 'Candidate leader model' }).getByRole('option', { name: new RegExp(ineligible.model) }).first();
      await expect(opt).toBeDisabled();
      await expect(opt).toContainText('not eligible');
    }
  });

  test('safe switch: confirm lists the steps, the new leader validates, the top bar follows leader.changed', async ({ page }) => {
    const before = (await api<{ leader: { provider: string; model: string } }>('GET', '/leadership')).leader;
    try {
      await openDashboard(page, '#/leadership');
      await page.getByRole('combobox', { name: 'Candidate leader model' }).selectOption('zeta::zeta-model');
      await page.getByRole('button', { name: /Switch leader/ }).click();
      const dlg = page.getByRole('dialog');
      await expect(dlg.getByRole('list', { name: 'Switch steps' }).getByRole('listitem')).toHaveCount(6);
      await expect(dlg).toContainText('Running workers are unaffected');
      await dlg.getByRole('button', { name: 'Switch leader', exact: true }).click();
      await expect(dlg.getByTestId('switch-result')).toHaveAttribute('data-status', 'switched', { timeout: 60_000 });
      await expect(dlg.getByTestId('switch-result')).toContainText('Validated by the new leader');
      await expect(page.getByTestId('leader')).toContainText('zeta / zeta-model');
      expect((await api<{ leader: { provider: string } }>('GET', '/leadership')).leader.provider).toBe('zeta');
      await dlg.getByRole('button', { name: 'Close', exact: true }).last().click();
      await expect(page.getByTestId('current-leader')).toContainText('zeta / zeta-model');
    } finally {
      await api('POST', '/leadership/switch', { provider: before.provider, model: before.model });
    }
  });

  test('a new leader that fails validation is rolled back and the reason is shown', async ({ page }) => {
    const before = (await api<{ leader: { provider: string; model: string } }>('GET', '/leadership')).leader;
    await openDashboard(page, '#/leadership');
    await page.getByRole('combobox', { name: 'Candidate leader model' }).selectOption('zulu::zulu-model');
    await page.getByRole('button', { name: /Switch leader/ }).click();
    const dlg = page.getByRole('dialog');
    await dlg.getByRole('button', { name: 'Switch leader', exact: true }).click();
    await expect(dlg.getByTestId('switch-result')).toHaveAttribute('data-status', 'rolled_back', { timeout: 60_000 });
    await expect(dlg.getByTestId('rollback-reason')).not.toBeEmpty();
    await dlg.getByRole('button', { name: 'Close', exact: true }).last().click();
    await expect(page.getByTestId('leader')).toContainText(`${before.provider} / ${before.model}`);
    expect((await api<{ leader: { provider: string } }>('GET', '/leadership')).leader.provider).toBe(before.provider);
  });

  test('settings save and persist; the permissions list is read-only', async ({ page }) => {
    await openDashboard(page, '#/leadership');
    await page.getByLabel('Consultations per day').fill('12');
    await page.getByRole('button', { name: /Save fallback/ }).click();
    await expect.poll(async () => (await api<{ settings: { max_consultations_per_day: number } }>('GET', '/leadership')).settings.max_consultations_per_day).toBe(12);
    await api('PUT', '/leadership', { max_consultations_per_day: 10 });
    await expect(page.getByRole('region', { name: 'Permissions and approvals' }).getByRole('checkbox')).toHaveCount(0);
  });

  test('ask advisor shows the structured answer, independence note and a recorded decision flagged without evidence', async ({ page }) => {
    await openDashboard(page, '#/leadership');
    await page.getByLabel('Problem').fill('Should the cache live in memory or on disk?');
    await page.getByRole('button', { name: 'Ask advisor' }).click();
    await expect(page.getByTestId('recommended')).toHaveText('Keep it local', { timeout: 30_000 });
    await expect(page.getByTestId('independence')).toHaveAttribute('data-independence', /same_provider|different_provider/);
    const rows = page.getByTestId('decision-row');
    await expect(rows.filter({ hasText: 'Should the cache live' }).first()).toBeVisible();
    await rows.filter({ hasText: 'Should the cache live' }).first().click();
    await expect(page.getByTestId('decision-detail').getByTestId('evidence-status')).toHaveAttribute('data-status', 'none');
  });
});

test.describe('objectives', () => {
  test('a new objective is planned by the leader and shows deliverables and the task graph', async ({ page }) => {
    const name = uid('objproj');
    const project = await createProjectApi(name);
    await openDashboard(page, '#/mission');
    await page.getByRole('button', { name: 'New objective' }).click();
    const dlg = page.getByRole('dialog');
    await dlg.getByLabel('Project').selectOption(project.id);
    await expect(dlg.getByRole('checkbox', { name: /Start the tasks/ })).not.toBeChecked();
    await dlg.getByLabel('Objective').fill('Build an exporter for the report data');
    await dlg.getByRole('button', { name: 'Plan objective' }).click();
    const detail = page.getByRole('dialog');
    await expect(detail.getByTestId('task-graph')).toBeVisible({ timeout: 30_000 });
    await expect(detail).toContainText('A working exporter');
    await expect(detail).toContainText('Node installed');
    await expect(detail.getByTestId('objective-status')).toHaveAttribute('data-status', 'planned');
    const items = detail.getByTestId('objective-task');
    await expect(items).toHaveCount(3);
    expect(await items.evaluateAll((els) => els.map((e) => e.getAttribute('data-layer')))).toEqual(['0', '1', '2']);
    // not started: nothing runs until the user starts the tasks
    const tasks = await api<{ title: string; state: string; objective_id: string | null }[]>('GET', `/tasks?project_id=${project.id}`);
    expect(tasks).toHaveLength(3);
    expect(tasks.every((t) => t.state === 'CREATED' && t.objective_id)).toBe(true);
    // a task opens from the graph and links back to the objective
    await items.first().getByRole('button').click();
    await expect(page.getByTestId('objective-link')).toBeVisible();
  });

  test('a failed plan shows the backend reason', async ({ page }) => {
    const project = await createProjectApi(uid('badplan'));
    await openDashboard(page, '#/mission');
    await page.getByRole('button', { name: 'New objective' }).click();
    const dlg = page.getByRole('dialog');
    await dlg.getByLabel('Project').selectOption(project.id);
    await dlg.getByLabel('Objective').fill('MOCK:BADPLAN do something');
    await dlg.getByRole('button', { name: 'Plan objective' }).click();
    await expect(page.getByTestId('objective-failed')).toContainText('decomposition is invalid', { timeout: 30_000 });
  });
});
