import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { api, createProjectApi, createTaskApi, openDashboard, uid } from './support/helpers';

// Pixel Office against the real backend (mock provider): a running task must be visible as agent activity.
const OUT = path.resolve(import.meta.dirname ?? '.', '..', '..', 'docs', 'screenshots');
test.use({ viewport: { width: 1600, height: 900 } });

interface ApiAgent {
  id: string;
  activity: string;
  current_task_id: string | null;
}

test('the office renders and an agent seat reflects a running task', async ({ page }) => {
  const project = await createProjectApi(uid('office-app'));
  const title = uid('Office task');
  const task = await createTaskApi(project.id, title, { description: 'MOCK:HANG', autostart: true });
  // the task keeps running until it is cancelled, so the office has something to show
  await expect
    .poll(async () => (await api<{ state: string }>('GET', `/tasks/${task.id}`)).state, { timeout: 30_000 })
    .toMatch(/PLANNING|RUNNING/);

  try {
    await openDashboard(page, '#/office');
    const canvas = page.getByTestId('office-canvas');
    await expect(canvas).toBeVisible({ timeout: 30_000 });

    // Non-blank readback: copy the (WebGL or 2D) canvas into a 2D canvas and count distinct colours.
    await expect
      .poll(
        () =>
          page.evaluate(async () => {
            const src = document.querySelector<HTMLCanvasElement>('[data-testid="office-canvas"]');
            if (!src || !src.width || !src.height) return 0;
            await new Promise((r) => requestAnimationFrame(() => r(null)));
            const c = document.createElement('canvas');
            c.width = Math.min(src.width, 800);
            c.height = Math.min(src.height, 600);
            const ctx = c.getContext('2d');
            if (!ctx) return 0;
            ctx.drawImage(src, 0, 0, c.width, c.height);
            const d = ctx.getImageData(0, 0, c.width, c.height).data;
            const colours = new Set<number>();
            for (let i = 0; i < d.length; i += 16) colours.add((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]);
            return colours.size;
          }),
        { timeout: 30_000, message: 'the office canvas stays blank' },
      )
      .toBeGreaterThan(8);

    // An agent seat reflects the running task: the offscreen agent list mirrors the canvas with data-activity.
    await expect
      .poll(
        async () => {
          const agents = await api<ApiAgent[]>('GET', '/agents');
          const busy = agents.find((a) => a.current_task_id === task.id);
          return busy ? busy.activity : 'none';
        },
        { timeout: 30_000 },
      )
      .not.toMatch(/^(none|idle)$/);
    const busy = (await api<ApiAgent[]>('GET', '/agents')).find((a) => a.current_task_id === task.id)!;
    const seat = page.locator(`[data-testid="office-agent"][data-agent-id="${busy.id}"]`);
    await expect(seat).toHaveAttribute('data-task-id', task.id, { timeout: 30_000 });
    await expect(seat).not.toHaveAttribute('data-activity', 'idle');

    mkdirSync(OUT, { recursive: true });
    await page.screenshot({ path: path.join(OUT, 'dashboard-pixel-office.png') });
  } finally {
    await api('POST', `/tasks/${task.id}/cancel`).catch(() => undefined);
  }
});
