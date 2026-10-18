import { type Page, type Route, expect, test } from "@playwright/test";

// The push channel end to end. Two facts, both about timers: nothing refetches on its own inside
// the window this spec watches (the interval polls are gone), and a change frame on the stream
// makes the fleet list refetch at once. Everything the visited page fetches is route-mocked, the
// same discipline smoke.spec.ts follows.

const SILENT_STREAM = "retry: 100000\n\n";

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function sse(route: Route, body: string) {
  return route.fulfill({ status: 200, contentType: "text/event-stream", body });
}

async function mockApi(page: Page, counter: { runs: number }, stream: string) {
  await page.route("**/api/events**", (route) => sse(route, stream));
  await page.route("**/api/runs**", (route) => {
    counter.runs += 1;
    return json(route, { runs: [] });
  });
  await page.route("**/api/runs/recent**", (route) => json(route, { runs: [] }));
  await page.route("**/api/sessions**", (route) => json(route, { sessions: [] }));
  await page.route("**/api/library", (route) => json(route, []));
  await page.route("**/api/schedules", (route) => json(route, { schedules: [] }));
  await page.route("**/api/harness/*", (route) => json(route, {}));
}

test("the fleet board does not poll", async ({ page }) => {
  const counter = { runs: 0 };
  await mockApi(page, counter, SILENT_STREAM);
  await page.goto("/workflows");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  // The mount fetch itself lands a moment after the heading paints, so wait for it before taking
  // the reading a poll would grow from.
  await expect.poll(() => counter.runs).toBeGreaterThan(0);
  const afterMount = counter.runs;
  await page.waitForTimeout(6000);
  expect(counter.runs).toBe(afterMount);
});

test("a change frame makes the fleet board refetch", async ({ page }) => {
  const counter = { runs: 0 };
  const frame = `event: change\ndata: ${JSON.stringify({ topic: "run.wf.demo.v1", at: "2026-08-26T12:00:00Z" })}\n\n`;
  await mockApi(page, counter, frame);
  await page.goto("/workflows");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect.poll(() => counter.runs, { timeout: 10000 }).toBeGreaterThan(1);
});
