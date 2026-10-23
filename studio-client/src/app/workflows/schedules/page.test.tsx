import { screen } from "@testing-library/dom";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { Schedule } from "@/api/schedules";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the page's rows see the mocked module.
import WorkflowSchedulesPage from "./page";

// The page is a thin mount of `SchedulesPage`; this confirms the wiring reaches a real
// screen -- the station body's own behavior is covered in `schedules-page.test.tsx`.

const BASE = "http://studio.test";

const SCHEDULE: Schedule = {
  id: "sched-1",
  workflow_id: "wf.sales.v1",
  arguments: {},
  cadence: "every 30m",
  enabled: true,
  gate_policy: "wait",
  gate_mode: "wait",
  capacity_key: null,
  capacity_limit: null,
  blackout: null,
  catch_up: false,
  session_id: "",
  created_at: "2026-08-23T10:00:00Z",
  last_run_at: null,
  last_outcome: "",
  next_run_at: "2026-08-23T10:30:00Z",
  live: null,
  paused_at: null,
  spend_today_usd: "0",
  spend_week_usd: "0",
  spend_today_unpriced: 0,
  spend_week_unpriced: 0,
};

describe("WorkflowSchedulesPage", () => {
  it("renders the schedules heading, the New schedule action, and a stored schedule", async () => {
    server.use(
      http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules: [SCHEDULE] })),
      http.get(`${BASE}/api/library`, () => HttpResponse.json([])),
    );
    render(<WorkflowSchedulesPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Schedules" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New schedule" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "wf.sales.v1" })).toHaveAttribute(
      "href",
      "/workflows/wf.sales.v1",
    );
  });
});
