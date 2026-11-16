import { defineConfig, devices } from '@playwright/test'

/**
 * Visual + layout checks for the Command Bridge.
 *
 * The spec mocks the HTTP API with fixtures (no backend needed) and takes the screenshots that are
 * committed under `docs/design/`. Run it with a browser available:
 *
 *   npx playwright install chromium
 *   npm run test:e2e            # writes docs/design/bridge-*.png
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'off',
    video: 'off',
    // Optional: point the suite at a Chromium you already have (`E2E_CHROMIUM=/path/to/chrome`).
    launchOptions: process.env.E2E_CHROMIUM
      ? { executablePath: process.env.E2E_CHROMIUM, args: ['--no-sandbox', '--disable-dev-shm-usage'] }
      : {},
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 4173',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: true,
    timeout: 180_000,
  },
})
