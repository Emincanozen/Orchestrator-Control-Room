import { defineConfig } from '@playwright/test';

// E2E runs against the REAL backend (mock provider only) serving the built dashboard.
// `npm run e2e` builds the frontend first; global setup/teardown start and stop the backend
// on a temp data dir, so the run is repeatable and leaves no processes behind.
// Port: E2E_PORT (default 8811). Browsers are not installed automatically:
//   npx playwright install chromium
const port = Number(process.env.E2E_PORT ?? 8811);

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  workers: 1, // one shared backend; RAM is tight
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${port}`, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
