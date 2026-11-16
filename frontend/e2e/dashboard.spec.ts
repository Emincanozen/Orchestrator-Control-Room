import { expect, test } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { restartBackend, token } from './support/backend';
import {
  api,
  closeDrawer,
  column,
  createProjectApi,
  createTaskApi,
  expectOnBoard,
  makeGitRepo,
  makePlainDir,
  openDashboard,
  openTask,
  tab,
  taskCard,
  taskState,
  uid,
  type ApiTask,
} from './support/helpers';

const PASS_VERIFY = `node -e "process.exit(require('fs').existsSync('result.txt')?0:1)"`;
const FAIL_VERIFY = 'node -e process.exit(1)';

test.afterEach(async ({}, info) => {
  if (info.status === info.expectedStatus) return;
  // On failure, attach what the backend itself believes (tasks, approvals) to explain a UI mismatch.
  try {
    const tasks = await api<ApiTask[]>('GET', '/tasks');
    const approvals = await api<{ kind: string; status: string; summary: string }[]>('GET', '/approvals');
    const summary = { tasks: tasks.map((x) => ({ title: x.title, state: x.state, reason: (x as unknown as { state_reason: string }).state_reason })), approvals };
    console.log('BACKEND STATE', JSON.stringify(summary, null, 1));
  } catch (e) {
    console.log('backend state unavailable', String(e));
  }
});

test.describe('authorisation', () => {
  test('without a token the dashboard shows the unauthorised screen and loads no data', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Not authorised' })).toBeVisible();
    await expect(page.getByText('needs the local API token')).toBeVisible();
    await expect(page.getByTestId('conn-status')).toHaveCount(0);
  });

  test('a wrong token is rejected by the backend and reported', async ({ page }) => {
    await page.goto('/?token=definitely-not-the-token');
    await expect(page.getByRole('heading', { name: 'Not authorised' })).toBeVisible();
    await expect(page.getByText('rejected the access token')).toBeVisible();
  });

  test('with the token the board opens, the token leaves the address bar and the socket goes live', async ({ page }) => {
    await page.goto(`/?token=${encodeURIComponent(token())}`);
    await expect(page.getByRole('heading', { name: 'Mission Control' })).toBeVisible();
    await expect(page.getByTestId('conn-status')).toContainText('live');
    expect(page.url()).not.toContain('token=');
    await expect(page.getByTestId('leader')).toBeVisible();
  });
});

test.describe('projects and the task lifecycle', () => {
  test('add a project through the UI, then create, start and watch a task complete', async ({ page }) => {
    const name = uid('proj');
    const dir = makeGitRepo(name);
    const title = uid('slow');
    await openDashboard(page);

    // add project
    await page.getByRole('button', { name: 'Add project' }).click();
    await page.getByRole('dialog').getByLabel('Name').fill(name);
    await page.getByRole('dialog').getByLabel('Working directory path').fill(dir);
    await page.getByRole('button', { name: 'Add project', exact: true }).last().click();
    await expect(page.getByRole('button', { name: `Edit ${name}` })).toBeVisible();

    // create a task with a verify command 
    await page.getByRole('button', { name: 'New task' }).click();
    await page.getByRole('dialog').getByLabel('Title').fill(title);
    await page.getByRole('dialog').getByLabel('Description').fill('MOCK:SLOW write result.txt');
    await page.getByRole('dialog').getByLabel('Project', { exact: true }).selectOption({ label: name });
    await page.getByRole('dialog').getByLabel('Verify commands', { exact: true }).fill(PASS_VERIFY);
    await page.getByRole('dialog').getByRole('button', { name: 'Add Verify commands' }).click();
    // free text while the model registry is unavailable, a select once /models exists
    const providerField = page.getByRole('dialog').getByLabel('Preferred provider (optional)');
    if ((await providerField.evaluate((e) => e.tagName)) === 'SELECT') await providerField.selectOption('mock');
    else await providerField.fill('mock');
    await page.getByRole('dialog').getByLabel('Queue immediately after creation').check();
    await page.getByRole('button', { name: 'Create task' }).click();

    // the drawer opens on the new task: live log shows mock output while it runs
    await expect(page.getByRole('dialog')).toBeVisible();
    await tab(page, 'Live log').click();
    await expect(page.getByRole('log', { name: 'Live task output' })).toContainText('slow task: reading the project');
    await expect(page.getByRole('log', { name: 'Live task output' })).toContainText('read_file');
    await expect(page.getByRole('log', { name: 'Live task output' })).toContainText('wrote result.txt [result.txt]');

    // the state moved through the board to COMPLETED
    await expect(page.getByTestId('overview-verification')).toHaveCount(0); // not on this tab
    await closeDrawer(page);
    await expectOnBoard(page, title, 'COMPLETED');
    await expect(column(page, 'RUNNING').getByRole('button', { name: `Open task ${title}` })).toHaveCount(0);

    // task detail: timeline, runs, verification, diff, artifacts, checkpoints
    await openTask(page, title);
    await tab(page, 'Timeline').click();
    const timeline = page.getByTestId('timeline');
    for (const state of ['QUEUED', 'RUNNING', 'TESTING', 'COMPLETED']) await expect(timeline).toContainText(state);

    await tab(page, 'Runs').click();
    await expect(page.getByRole('cell', { name: 'mock / mock-model' }).first()).toBeVisible();
    await expect(page.getByRole('cell', { name: 'succeeded' }).first()).toBeVisible();

    await tab(page, 'Verification').click();
    await expect(page.getByLabel('Verification passed')).toBeVisible();
    await expect(page.getByText('PASS').first()).toBeVisible();

    await tab(page, 'Diff').click();
    await expect(page.getByTestId('changed-files')).toContainText('result.txt');
    await expect(page.getByTestId('diff')).toContainText('+hello from the mock provider');

    await tab(page, 'Artifacts').click();
    await page.getByRole('button', { name: /result\.txt/ }).click();
    await expect(page.getByTestId('artifact-content')).toHaveText(/hello from the mock provider/);

    await tab(page, 'Checkpoints').click();
    await expect(page.getByRole('form', { name: 'Create checkpoint' })).toBeVisible();

    await tab(page, 'Overview').click();
    await expect(page.getByTestId('overview-verification')).toHaveText('passed');
    await expect(page.getByTestId('provider-model')).toContainText('mock');
  });

  test('a task whose verification fails ends FAILED (never COMPLETED) and can be retried to success', async ({ page }) => {
    const project = await createProjectApi(uid('failproj'));
    const title = uid('failing');
    const task = await createTaskApi(project.id, title, { verify_commands: [{ file_exists: 'ready.txt' }], autostart: true });
    await openDashboard(page);

    await expectOnBoard(page, title, 'FAILED');
    await expect(column(page, 'COMPLETED').getByRole('button', { name: `Open task ${title}` })).toHaveCount(0);
    expect(await taskState(task.id)).toBe('FAILED');

    await openTask(page, title);
    await tab(page, 'Verification').click();
    await expect(page.getByLabel('Verification failed').first()).toBeVisible();
    await expect(page.getByText('FAIL').first()).toBeVisible();
    await closeDrawer(page);

    // "the user fixes the problem" in the task's workspace, then retries
    const full = await api<ApiTask>('GET', `/tasks/${task.id}`);
    expect(full.worktree_path).toBeTruthy();
    writeFileSync(path.join(full.worktree_path as string, 'ready.txt'), 'ok\n');
    await page.getByRole('button', { name: `Retry task ${title}` }).click();
    await expectOnBoard(page, title, 'COMPLETED');
  });

  test('a failing verify command typed in the form fails the task and a retry fails again, visibly', async ({ page }) => {
    // This case isolates a native command failure plus explicit manual retry. Automatic
    // repair limits have separate backend coverage; four cold runtime validations are
    // not a 30-second UI contract. Restore the effective policy even when an assertion fails.
    const settings = await api<{ effective: { limits: { max_repair_cycles: number } } }>('GET', '/settings');
    const previousRepairCycles = settings.effective.limits.max_repair_cycles;
    await api('PUT', '/settings', { values: { limits: { max_repair_cycles: 0 } } });
    try {
      const project = await createProjectApi(uid('failproj2'));
      const title = uid('failcmd');
      const task = await createTaskApi(project.id, title, { verify_commands: [FAIL_VERIFY], autostart: true });
      await openDashboard(page);
      await expectOnBoard(page, title, 'FAILED');
      await page.getByRole('button', { name: `Retry task ${title}` }).click();
      // it re-enters the pipeline and lands in FAILED again; the timeline shows two QUEUED transitions
      await expect.poll(async () => (await api<{ to_state: string }[]>('GET', `/tasks/${task.id}/transitions`)).filter((t) => t.to_state === 'QUEUED').length, { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
      await expectOnBoard(page, title, 'FAILED');
      await openTask(page, title);
      await tab(page, 'Timeline').click();
      await expect(page.getByTestId('timeline')).toContainText('FAILED');
      await expect.poll(async () => (await api<{ to_state: string }[]>('GET', `/tasks/${task.id}/transitions`)).filter((t) => t.to_state === 'FAILED').length, { timeout: 30_000 }).toBe(2);
      expect((await api<ApiTask & { repair_cycles: number }>('GET', `/tasks/${task.id}`)).repair_cycles).toBe(0);
    } finally {
      await api('PUT', '/settings', { values: { limits: { max_repair_cycles: previousRepairCycles } } });
    }
  });

  test('a hanging task can be cancelled', async ({ page }) => {
    const project = await createProjectApi(uid('hangproj'));
    const title = uid('hang');
    await createTaskApi(project.id, title, { description: 'MOCK:HANG', verify_commands: [PASS_VERIFY], autostart: true });
    await openDashboard(page);
    await expectOnBoard(page, title, 'PLANNING');
    await expect(page.getByTestId('running-count')).not.toHaveText(/running\s*0/);
    await page.getByRole('button', { name: `Cancel task ${title}` }).click();
    await expectOnBoard(page, title, 'CANCELLED');
    await expect(column(page, 'PLANNING').getByRole('button', { name: `Open task ${title}` })).toHaveCount(0);
  });

  test('manual checkpoint on a paused task can be created and restored behind a confirm dialog', async ({ page }) => {
    const project = await createProjectApi(uid('cpproj'));
    const title = uid('checkpoint');
    const task = await createTaskApi(project.id, title, { description: 'MOCK:HANG', verify_commands: [PASS_VERIFY], autostart: true });
    await openDashboard(page);
    await expectOnBoard(page, title, 'PLANNING');
    await page.getByRole('button', { name: `Pause task ${title}` }).click();
    await expectOnBoard(page, title, 'PAUSED');

    await openTask(page, title);
    await tab(page, 'Checkpoints').click();
    await page.getByLabel('Label (optional)').fill('before edit');
    await page.getByRole('button', { name: 'Create checkpoint' }).click();
    const manual = page.getByTestId('checkpoint').filter({ hasText: 'manual' });
    await expect(manual.first()).toBeVisible();
    await expect(manual.first()).toContainText('before edit');

    // change the workspace, then restore
    const wt = (await api<ApiTask>('GET', `/tasks/${task.id}`)).worktree_path as string;
    const stray = path.join(wt, 'stray.txt');
    writeFileSync(stray, 'x');
    await manual.first().getByRole('button', { name: /Restore checkpoint/ }).click();
    const confirm = page.getByRole('dialog', { name: 'Restore checkpoint?' });
    await expect(confirm).toBeVisible();
    await confirm.getByRole('button', { name: 'Cancel' }).click();
    await expect(confirm).toBeHidden();
    await manual.first().getByRole('button', { name: /Restore checkpoint/ }).click();
    await page.getByRole('button', { name: 'Restore worktree' }).click();
    await expect(page.getByRole('dialog', { name: 'Restore checkpoint?' })).toBeHidden();
    await expect(page.getByRole('alert')).toHaveCount(0);

    await api('POST', `/tasks/${task.id}/cancel`);
  });

  test('an approval appears and can be resolved; the unverifiable task completes only after approval', async ({ page }) => {
    // 1. registering a plain directory raises git_init_in_project
    const plain = await createProjectApi(uid('plain'), makePlainDir(uid('plaindir')));
    expect(plain.is_git).toBe(false);
    // 2. a task without verify commands needs approval to be accepted
    const project = await createProjectApi(uid('approvproj'));
    const title = uid('unverifiable');
    await createTaskApi(project.id, title, { autostart: true });

    await openDashboard(page);
    await expectOnBoard(page, title, 'NEEDS_APPROVAL');
    await expect(page.getByTestId('approvals-badge')).not.toHaveAccessibleName('0 pending approvals');

    await page.getByRole('link', { name: /pending approvals/ }).click();
    await expect(page.getByRole('heading', { name: 'Approvals' })).toBeVisible();
    const taskApproval = page.getByRole('listitem', { name: 'Approval unverifiable_completion' }).filter({ hasText: title });
    await expect(taskApproval).toBeVisible();
    await expect(page.getByRole('listitem', { name: /Approval git_init_in_project/ }).first()).toBeVisible();

    await taskApproval.getByLabel(/Note for approval/).fill('looks fine');
    await taskApproval.getByRole('button', { name: /Approve/ }).click();
    await expect(page.getByRole('listitem', { name: 'Approval unverifiable_completion' }).filter({ hasText: title })).toHaveCount(0);
    await expect(page.getByText('Resolved this session')).toBeVisible();

    // deny the git init so nothing touches the plain directory
    const gitInit = page.getByRole('listitem', { name: /Approval git_init_in_project/ }).filter({ hasText: 'plaindir' });
    if (await gitInit.count()) {
      await gitInit.first().getByRole('button', { name: /Deny/ }).click();
    }

    await page.getByRole('link', { name: 'Mission Control' }).click();
    await expectOnBoard(page, title, 'COMPLETED');
  });
});

test.describe('persistence and reconnect', () => {
  test('a page reload restores everything from the backend, nothing is lost', async ({ page }) => {
    const project = await createProjectApi(uid('reloadproj'));
    const done = uid('done');
    const failed = uid('failed');
    const paused = uid('paused');
    await createTaskApi(project.id, done, { verify_commands: [PASS_VERIFY], autostart: true });
    await createTaskApi(project.id, failed, { verify_commands: [FAIL_VERIFY], autostart: true });
    const p = await createTaskApi(project.id, paused, { description: 'MOCK:HANG', verify_commands: [PASS_VERIFY], autostart: true });
    await openDashboard(page);
    await expectOnBoard(page, done, 'COMPLETED');
    await expectOnBoard(page, failed, 'FAILED');
    await expectOnBoard(page, paused, 'PLANNING');
    await page.getByRole('button', { name: `Pause task ${paused}` }).click();
    await expectOnBoard(page, paused, 'PAUSED');

    await page.reload();
    await expect(page.getByTestId('conn-status')).toContainText('live');
    await expectOnBoard(page, done, 'COMPLETED');
    await expectOnBoard(page, failed, 'FAILED');
    await expectOnBoard(page, paused, 'PAUSED');
    // history of a finished task is still there: run, verification and the log
    await openTask(page, done);
    await tab(page, 'Verification').click();
    await expect(page.getByLabel('Verification passed')).toBeVisible();
    await tab(page, 'Live log').click();
    await expect(page.getByRole('log', { name: 'Live task output' })).toContainText('mock work finished');
    await closeDrawer(page);
    await api('POST', `/tasks/${p.id}/cancel`);
  });

  test('the active duration of a task that was already running at load time ticks from its real start', async ({ page }) => {
    const project = await createProjectApi(uid('clockproj'));
    const title = uid('clock');
    const task = await createTaskApi(project.id, title, { description: 'MOCK:HANG', verify_commands: [PASS_VERIFY], autostart: true });
    await expect.poll(() => taskState(task.id), { timeout: 20_000 }).toBe('PLANNING');
    await page.waitForTimeout(4000); // the task has been active for a few seconds before the page ever loads
    await openDashboard(page);
    await expectOnBoard(page, title, 'PLANNING');
    const card = column(page, 'PLANNING').getByRole('listitem').filter({ has: taskCard(page, title) });
    // starts at (>= 4 s), not at 00:00 as it would if the clock started at page load
    await expect(card).toContainText(/(?<!\d)0[0-9]:[0-9]{2}(?!\d)/);
    const secondsOnCard = async () => {
      const m = /(\d{2}):(\d{2})(?!\d)/.exec((await card.innerText()) ?? '');
      return m ? Number(m[1]) * 60 + Number(m[2]) : -1;
    };
    await expect.poll(secondsOnCard, { timeout: 10_000 }).toBeGreaterThanOrEqual(4);
    const a = await secondsOnCard();
    await page.waitForTimeout(2500);
    expect(await secondsOnCard()).toBeGreaterThan(a); // and it keeps ticking
    await api('POST', `/tasks/${task.id}/cancel`);
  });

  test('settings: policy limits are validated, saved to the backend and survive a reload', async ({ page }) => {
    await openDashboard(page, '#/settings');
    const workers = page.getByLabel(/Max parallel workers/);
    await expect(workers).toBeVisible();
    await workers.fill('0');
    await expect(page.getByText('Must be greater than zero')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save limits' })).toBeDisabled();
    await workers.fill('4');
    await page.getByRole('button', { name: 'Save limits' }).click();
    await expect(page.getByRole('status')).toContainText('Saved');
    const settings = await api<{ effective: { limits: { max_parallel_workers: number } }; overrides: { limits: { max_parallel_workers: number } } }>('GET', '/settings');
    expect(settings.effective.limits.max_parallel_workers).toBe(4);
    await page.reload();
    await expect(page.getByLabel(/Max parallel workers/)).toHaveValue('4');
    await page.getByRole('button', { name: 'Reset to defaults' }).click();
    await expect(page.getByLabel(/Max parallel workers/)).toHaveValue('3');
  });

  test('after the backend process restarts the socket reconnects and the UI stays consistent', async ({ page }) => {
    const project = await createProjectApi(uid('restartproj'));
    const before = uid('before');
    await createTaskApi(project.id, before, { verify_commands: [PASS_VERIFY], autostart: true });
    await openDashboard(page);
    await expectOnBoard(page, before, 'COMPLETED');
    const seqBefore = (await api<{ last_seq: number }>('GET', '/status')).last_seq;

    await restartBackend(1500);
    // the dashboard noticed and recovered by itself, with no reload
    await expect(page.getByTestId('conn-status')).toContainText('live', { timeout: 40_000 });
    expect((await api<{ last_seq: number }>('GET', '/status')).last_seq).toBeGreaterThanOrEqual(seqBefore);
    await expectOnBoard(page, before, 'COMPLETED');
    await expect(taskCard(page, before)).toHaveCount(1); // not duplicated by the replay

    // and live updates flow again: a task created after the restart runs to completion on screen
    const after = uid('after');
    await createTaskApi(project.id, after, { verify_commands: [PASS_VERIFY], autostart: true });
    await expectOnBoard(page, after, 'COMPLETED');
    await expect(taskCard(page, after)).toHaveCount(1);

    // a fresh load agrees with the live view
    const live = await column(page, 'COMPLETED').getByRole('button').allInnerTexts();
    await page.reload();
    await expect(page.getByTestId('conn-status')).toContainText('live');
    await expectOnBoard(page, after, 'COMPLETED');
    const fresh = await column(page, 'COMPLETED').getByRole('button').allInnerTexts();
    expect(fresh.sort()).toEqual(live.sort());
  });
});
