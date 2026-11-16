import { expect, test } from '@playwright/test';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { api, closeDrawer, createProjectApi, createTaskApi, expectOnBoard, openDashboard, uid } from './support/helpers';

interface ApiSkill {
  id: string;
  state: string;
}

test.describe('providers view', () => {
  test('lists the mock provider with its real payload', async ({ page }) => {
    const real = (
      await api<{ provider: string; version: string | null; billing_class: string; authenticated: boolean | null }[]>('GET', '/providers')
    ).find((p) => p.provider === 'mock')!;
    expect(real).toBeTruthy();
    await openDashboard(page, '#/providers');
    const card = page.getByTestId('provider-mock');
    await expect(card).toBeVisible();
    await expect(card).toContainText('installed');
    await expect(card.getByTestId('auth-mock')).toContainText(real.authenticated === true ? 'Yes' : real.authenticated === false ? 'No' : 'Cannot be determined');
    if (real.version) await expect(card).toContainText(real.version);
    await expect(card).toContainText(real.billing_class === 'local' ? 'local' : real.billing_class);
    await expect(card.locator('[data-cap]').first()).toBeVisible();
    await expect(page.getByRole('table', { name: 'Models' })).toContainText('mock-model');
    // the mock reports no quota: whatever is shown for unknown usage must never be a number
    for (const u of await page.getByTestId('usage-unknown').all()) await expect(u).toContainText('Unknown');
    await page.getByRole('button', { name: /Refresh/ }).click();
    await expect(card).toBeVisible();
  });
});

test.describe('routing view', () => {
  test('preview explains the decision, and a finished task has an explained decision', async ({ page }) => {
    await openDashboard(page, '#/routing');
    await page.getByLabel('Title', { exact: true }).fill('Implement a small parser');
    await page.getByRole('button', { name: /Preview/ }).click();
    const region = page.getByRole('region', { name: 'Preview result' });
    await expect(region.getByTestId('outcome')).toHaveAttribute('data-outcome', 'route');
    await expect(region).toContainText('mock / mock-model');
    await expect(region.getByTestId('cls-type')).toBeVisible();
    await expect(region.getByTestId('candidate').first()).toBeVisible();

    const project = await createProjectApi(uid('routeproj'));
    const title = uid('routed');
    const task = await createTaskApi(project.id, title, { autostart: true, verify_commands: [{ file_exists: 'result.txt' }] });
    await openDashboard(page, '#/mission');
    await expectOnBoard(page, title, 'COMPLETED');
    await page.goto(`/#/routing?task=${task.id}`);
    const row = page.getByTestId('decision').first();
    await expect(row).toContainText('mock / mock-model');
    await row.getByRole('button').click();
    const x = row.getByTestId('explanation');
    await expect(x.getByTestId('candidate').first()).toContainText('mock-model');
    await expect(x.getByLabel('Reasons')).toBeVisible();
  });

  test('preferences: disable the mock provider, preview says blocked, reset restores it', async ({ page }) => {
    await openDashboard(page, '#/routing');
    const section = page.getByRole('region', { name: 'Disabled providers' });
    await section.getByLabel('Disable mock').check();
    await section.getByRole('button', { name: 'Save Disabled providers' }).click();
    await expect(section).toHaveAttribute('data-overridden', 'true');
    await page.getByLabel('Title', { exact: true }).fill('Anything at all');
    await page.getByRole('button', { name: /Preview/ }).click();
    const region = page.getByRole('region', { name: 'Preview result' });
    await expect(region.getByTestId('outcome')).toHaveAttribute('data-outcome', 'blocked');
    await expect(region.getByTestId('no-candidate')).toBeVisible();
    await expect(region.getByTestId('rejected-candidate').filter({ hasText: 'mock / mock-model' })).toContainText('Disabled by a routing preference');
    await section.getByRole('button', { name: 'Reset Disabled providers to default' }).click();
    await expect(section).toHaveAttribute('data-overridden', 'false');
    expect((await api<{ disabled_providers: string[] }>('GET', '/routing/preferences')).disabled_providers).toEqual([]);
  });
});

test.describe('skills manager', () => {
  test('catalogue lists entries and filters work', async ({ page }) => {
    await openDashboard(page, '#/skills');
    for (const id of ['e2e-alpha', 'e2e-nosrc', 'e2e-docs']) await expect(page.locator(`[data-skill-id="${id}"]`)).toBeVisible();
    await page.getByLabel('Search skills').fill('nosrc');
    await expect(page.getByTestId('skill-card')).toHaveCount(1);
    await page.getByLabel('Search skills').fill('');
    await page.getByLabel('Filter by kind').selectOption('docs');
    await expect(page.getByTestId('skill-card')).toHaveCount(1);
  });

  test('download, activate (approval required), approve, activate, files on disk, deactivate', async ({ page }) => {
    const project = await createProjectApi(uid('skillproj'));
    await openDashboard(page, '#/skills');
    await page.getByLabel('Show activation for project').selectOption(project.id);
    await page.getByRole('button', { name: 'Open skill e2e-alpha' }).click();
    const detail = page.getByTestId('skill-detail');
    await expect(detail).toContainText('Catalogued');
    await expect(detail.getByTestId('codex-support')).toContainText('unsupported');

    // Download
    await detail.getByRole('button', { name: /Download/ }).click();
    await expect(page.getByRole('dialog').last()).toContainText('nothing from the package is executed');
    await page.getByRole('dialog').last().getByRole('button', { name: 'Download', exact: true }).click();
    await expect(page.locator('[data-skill-state="DOWNLOADED_AND_DISABLED"]').first()).toBeVisible();
    expect((await api<ApiSkill>('GET', '/skills/e2e-alpha')).state).toBe('DOWNLOADED_AND_DISABLED');

    // SKILL.md as plain text
    await detail.getByRole('button', { name: /Show SKILL.md/ }).click();
    await expect(page.getByTestId('skill-content')).toContainText('<b>not html</b>');
    await expect(page.getByTestId('skill-content').locator('b')).toHaveCount(0);

    // Activate -> approval required, nothing written
    const target = path.join(project.path, '.claude', 'skills');
    await detail.getByRole('button', { name: /^Activate/ }).click();
    await page.getByRole('dialog').last().getByRole('button', { name: 'Activate', exact: true }).click();
    await expect(detail.getByTestId('approval-required')).toBeVisible();
    expect(existsSync(target) ? readdirSync(target) : []).toEqual([]);

    // Approve in the Approvals view
    await detail.getByTestId('approvals-link').click();
    await expect(page.getByRole('heading', { name: 'Approvals' })).toBeVisible();
    const card = page.getByRole('listitem', { name: 'Approval untrusted_tool_install' });
    await expect(card.getByTestId('approval-facts')).toContainText('e2e-alpha');
    await card.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByText('Nothing waiting for approval')).toBeVisible();

    // Activate again -> files on disk
    await page.goto('/#/skills');
    await page.getByLabel('Show activation for project').selectOption(project.id);
    await page.getByRole('button', { name: 'Open skill e2e-alpha' }).click();
    await page.getByTestId('skill-detail').getByRole('button', { name: /^Activate/ }).click();
    await page.getByRole('dialog').last().getByRole('button', { name: 'Activate', exact: true }).click();
    await expect(page.locator('[data-skill-state="INSTALLED_AND_ACTIVE"]').first()).toBeVisible();
    const dirs = readdirSync(target);
    expect(dirs.length).toBe(1);
    expect(readFileSync(path.join(target, dirs[0], 'SKILL.md'), 'utf8')).toContain('e2e-alpha');
    await expect(page.getByTestId('active-here')).toContainText('Active in');

    // Deactivate removes the files, the state follows
    await page.getByTestId('skill-detail').getByRole('button', { name: /^Deactivate/ }).click();
    await page.getByRole('dialog').last().getByRole('button', { name: 'Deactivate', exact: true }).click();
    await expect(page.locator('[data-skill-state="DOWNLOADED_AND_DISABLED"]').first()).toBeVisible();
    expect(existsSync(path.join(target, dirs[0]))).toBe(false);
    expect((await api<ApiSkill>('GET', '/skills/e2e-alpha')).state).toBe('DOWNLOADED_AND_DISABLED');
    await expect(page.getByTestId('skill-event').first()).toBeVisible();
  });

  test('an entry with no source cannot be downloaded and shows candidates; docs are catalogue-only', async ({ page }) => {
    await openDashboard(page, '#/skills');
    await page.getByRole('button', { name: 'Open skill e2e-nosrc' }).click();
    const d = page.getByTestId('skill-detail');
    await expect(d.getByRole('button', { name: /Download/ })).toBeDisabled();
    await expect(d.getByTestId('not-installable')).toContainText('source URL unknown');
    await expect(d.getByTestId('candidates')).toContainText('nosrc-two');
    await closeDrawer(page);
    await page.getByRole('button', { name: 'Open skill e2e-docs' }).click();
    const docs = page.getByTestId('skill-detail');
    await expect(docs.getByTestId('catalogue-only')).toContainText('nothing to install');
    await expect(docs.getByRole('button', { name: /^Activate/ })).toHaveCount(0);
  });
});
