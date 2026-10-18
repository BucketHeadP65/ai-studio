import { defineConfig, devices } from "@playwright/test";

import { resolvePort } from "./e2e/port";

const PORT = resolvePort(process.env.PLAYWRIGHT_PORT);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // One worker even off CI: the dashboard talks to a single local job registry,
  // so parallel specs would contend over the same backend state.
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "on-first-retry",
    // The suite mocks the API via page.route, but the app's CSP connect-src does
    // not allowlist the mock origin — the renderer would block the fetch BEFORE
    // interception can fulfill it. CSP is not under test here; disable it inside
    // the e2e browser only.
    bypassCSP: true,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npx next dev -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL: "http://api.test",
    },
  },
});
