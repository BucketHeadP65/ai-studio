import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Schedule } from "@/api/schedules";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so ScheduleCard (each card navigates its own workflow) sees the
// mocked module.
import { SchedulesPage } from "./schedules-page";

// The schedules station body: renders every schedule off `schedulesApi.list()`, on schedules
// first (soonest fire first), then paused ones, each card's name joined from `studioApi.library()`
// by workflow id, the create route (`ScheduleForm` in standalone mode, posting every field it
// collects), the edit route (`ScheduleForm` on one schedule, patching only what changed), and the
// toggle/run-now/delete mutations. Every request the mount can fire is mocked here (MSW errors on
// anything left unhandled).

const BASE = "http://studio.test";

afterEach(() => {
  routerPush.mockClear();
});

function scheduleFixture(overrides: Partial<Schedule> = {}): Schedule {
  return {
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
    ...overrides,
  };
}

function mountApi(
  schedules: Schedule[],
  libraryCards: Array<{ id: string; title: string }> = [],
): void {
  server.use(
    http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules })),
    http.get(`${BASE}/api/library`, () =>
      HttpResponse.json(
        libraryCards.map((card) => ({
          archived_at: null,
          shape: { steps: 0, person_gates: 0, agent_gates: 0 },
          runs: { last: null },
          id: card.id,
          title: card.title,
          summary: "",
          provisional: false,
          score: null,
          trust: {
            approved_by: [],
            level: "unverified",
            runs_with_evidence: 0,
            failed_runs: 0,
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
          needs: [],
          missing: [],
          brings: [],
          version: 1,
        })),
      ),
    ),
  );
}

// A workflow that declares no parameters, as its detail route answers it.
function workflowDetail(id: string) {
  return {
    ok: true,
    id,
    name: "sales",
    version: 1,
    description: "",
    title: "Sales",
    summary: "",
    applicability: "",
    provisional: false,
    archived_at: null,
    graph: { name: "sales", version: 1, nodes: [], edges: [] },
    issues: [],
    fits: true,
    missing: [],
    brings: [],
    spec_yaml: "name: sales\n",
    staleness: [],
    lineage: null,
    birth: [],
    carried: null,
    predictions: null,
    prediction_record: null,
    trust: {
      approved_by: [],
      level: "unverified",
      runs_with_evidence: 0,
      failed_runs: 0,
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
  };
}

function mountForm(workflowId: string): void {
  server.use(
    http.get(`${BASE}/api/workflow/${workflowId}`, () =>
      HttpResponse.json(workflowDetail(workflowId)),
    ),
    http.post(`${BASE}/api/schedules/preview`, async ({ request }) => {
      const { cadence } = (await request.json()) as { cadence: string };
      if (/^(on |after )/.test(cadence)) return HttpResponse.json({ next: [] });
      return HttpResponse.json({ next: ["2026-08-24T00:00:00+00:00"] });
    }),
  );
}

describe("SchedulesPage", () => {
  it("shows every schedule's name joined from the library by workflow id", async () => {
    mountApi(
      [scheduleFixture({ id: "sched-1", workflow_id: "wf.sales.v1" })],
      [{ id: "wf.sales.v1", title: "Sales digest" }],
    );
    render(<SchedulesPage />);

    expect(await screen.findByRole("link", { name: "Sales digest" })).toBeInTheDocument();
  });

  it("falls back to the workflow id as the name when the library carries no title for it", async () => {
    mountApi([scheduleFixture({ id: "sched-1", workflow_id: "wf.ghost.v1" })], []);
    render(<SchedulesPage />);

    expect(await screen.findByRole("link", { name: "wf.ghost.v1" })).toBeInTheDocument();
  });

  it("orders enabled schedules first by soonest fire, then paused ones", async () => {
    mountApi([
      scheduleFixture({
        id: "sched-late",
        workflow_id: "wf.late.v1",
        enabled: true,
        next_run_at: "2026-08-23T12:00:00Z",
      }),
      scheduleFixture({
        id: "sched-paused",
        workflow_id: "wf.paused.v1",
        enabled: false,
        next_run_at: "2026-08-23T10:15:00Z",
      }),
      scheduleFixture({
        id: "sched-soon",
        workflow_id: "wf.soon.v1",
        enabled: true,
        next_run_at: "2026-08-23T10:15:00Z",
      }),
    ]);
    render(<SchedulesPage />);

    const links = await screen.findAllByRole("link", { name: /^wf\./ });
    expect(links.map((link) => link.textContent)).toEqual([
      "wf.soon.v1",
      "wf.late.v1",
      "wf.paused.v1",
    ]);
  });

  it("summarizes the count, the on and paused split, and the soonest fire", async () => {
    mountApi([
      scheduleFixture({ id: "sched-1", enabled: true, next_run_at: "2026-08-23T10:12:00Z" }),
      scheduleFixture({ id: "sched-2", enabled: true, next_run_at: "2026-08-23T11:00:00Z" }),
      scheduleFixture({ id: "sched-3", enabled: false }),
    ]);
    render(<SchedulesPage />);

    expect(await screen.findByText(/3 schedules, 2 on, 1 paused\./)).toBeInTheDocument();
    expect(screen.getByText(/Next fire/)).toBeInTheDocument();
  });

  it("drops the paused clause when none is paused", async () => {
    mountApi([
      scheduleFixture({ id: "sched-1", enabled: true }),
      scheduleFixture({ id: "sched-2", enabled: true }),
    ]);
    render(<SchedulesPage />);

    expect(await screen.findByText(/^2 schedules, 2 on\./)).toBeInTheDocument();
  });

  it("drops the on clause when none is on", async () => {
    mountApi([
      scheduleFixture({ id: "sched-1", enabled: false }),
      scheduleFixture({ id: "sched-2", enabled: false }),
    ]);
    render(<SchedulesPage />);

    expect(await screen.findByText("2 schedules, 2 paused.")).toBeInTheDocument();
  });

  it("shows No schedules yet as the summary and an empty state pointing at New schedule", async () => {
    mountApi([]);
    render(<SchedulesPage />);

    expect(await screen.findByText("No schedules yet.")).toBeInTheDocument();
    expect(screen.getByText("Nothing scheduled yet.")).toBeInTheDocument();
    expect(screen.getByText(/ask the agent in chat to schedule one/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "New schedule" })).toHaveLength(2);
  });

  it("opens the create form from New schedule and posts every field on submit", async () => {
    mountApi([], [{ id: "wf-sales", title: "Sales" }]);
    let posted: unknown = null;
    server.use(
      http.get(`${BASE}/api/workflow/wf-sales`, () =>
        HttpResponse.json({
          ok: true,
          id: "wf-sales",
          name: "sales",
          version: 1,
          description: "",
          title: "Sales",
          summary: "",
          applicability: "",
          provisional: false,
          archived_at: null,
          graph: { name: "sales", version: 1, nodes: [], edges: [] },
          issues: [],
          fits: true,
          missing: [],
          brings: [],
          spec_yaml: "name: sales\n",
          staleness: [],
          lineage: null,
          birth: [],
          carried: null,
          predictions: null,
          prediction_record: null,
          trust: {
            approved_by: [],
            level: "unverified",
            runs_with_evidence: 0,
            failed_runs: 0,
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        }),
      ),
      http.post(`${BASE}/api/schedules`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ schedule: scheduleFixture({ id: "sched-9" }) });
      }),
    );
    render(<SchedulesPage />);

    await screen.findByText("No schedules yet.");
    await userEvent.click(screen.getAllByRole("button", { name: "New schedule" })[0]);
    await userEvent.selectOptions(
      await screen.findByLabelText("Workflow"),
      screen.getByRole("option", { name: "Sales" }),
    );
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.type(screen.getByLabelText("Capacity key"), "gpu-pool");
    await userEvent.type(screen.getByLabelText("Capacity limit"), "3");
    await userEvent.type(screen.getByLabelText("Blackout windows"), "22:00-06:00");
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Catch up a missed fire when possible" }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(posted).toEqual({
        workflow_id: "wf-sales",
        arguments: {},
        cadence: "every 30m",
        gate_policy: "wait",
        gate_mode: "wait",
        capacity_key: "gpu-pool",
        capacity_limit: 3,
        blackout: "22:00-06:00",
        catch_up: true,
      }),
    );
  });

  it("toggles a schedule's enable switch without navigating", async () => {
    const schedules = [scheduleFixture({ enabled: true })];
    mountApi(schedules);
    server.use(
      http.patch(`${BASE}/api/schedules/sched-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ enabled: false });
        schedules[0] = { ...schedules[0], enabled: false };
        return HttpResponse.json({ schedule: schedules[0] });
      }),
    );
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("switch", { name: "Schedule on" }));

    expect(await screen.findByRole("switch", { name: "Schedule off" })).toBeInTheDocument();
  });

  it("fires a schedule's run now", async () => {
    mountApi([scheduleFixture()]);
    server.use(
      http.post(`${BASE}/api/schedules/sched-1/run-now`, () =>
        HttpResponse.json({ ok: true, outcome: "launched" }),
      ),
    );
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Run now" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Schedules" })).toBeInTheDocument();
  });

  it("deletes a schedule after confirming", async () => {
    let schedules = [scheduleFixture()];
    server.use(
      http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules })),
      http.get(`${BASE}/api/library`, () => HttpResponse.json([])),
      http.delete(`${BASE}/api/schedules/sched-1`, () => {
        schedules = [];
        return HttpResponse.json({ ok: true });
      }),
    );
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Delete schedule" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete it" }));

    await screen.findByText("No schedules yet.");
  });

  it("opens the edit form from a card's Edit, not the workflow", async () => {
    mountApi([scheduleFixture({ cadence: "after research-digest" })]);
    mountForm("wf.sales.v1");
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));

    expect(await screen.findByRole("heading", { name: "Edit schedule" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Event" })).toHaveValue("after research-digest");
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("saves an edit as a PATCH holding only the changed field, then shows the new value", async () => {
    const schedules = [scheduleFixture({ cadence: "every 30m" })];
    let patched: unknown = null;
    mountApi(schedules);
    mountForm("wf.sales.v1");
    server.use(
      http.get(`${BASE}/api/schedules`, () => HttpResponse.json({ schedules })),
      http.patch(`${BASE}/api/schedules/sched-1`, async ({ request }) => {
        patched = await request.json();
        schedules[0] = { ...schedules[0], cadence: "every 15m" };
        return HttpResponse.json({ schedule: schedules[0] });
      }),
    );
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    const count = await screen.findByLabelText("Interval count");
    await userEvent.clear(count);
    await userEvent.type(count, "15");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(patched).toEqual({ cadence: "every 15m" }));
    expect(await screen.findByText("every 15m")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a refused edit's reason in the form", async () => {
    mountApi([scheduleFixture()]);
    mountForm("wf.sales.v1");
    server.use(
      http.patch(`${BASE}/api/schedules/sched-1`, () =>
        HttpResponse.json(
          {
            ok: false,
            message:
              "a schedule of 'sales' cannot fire after 'sales', because every run that succeeds would fire it again",
          },
          { status: 400 },
        ),
      ),
    );
    render(<SchedulesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit schedule" }));
    await userEvent.click(await screen.findByRole("radio", { name: /On an event/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Event" }), "after sales");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "a schedule of 'sales' cannot fire after 'sales', because every run that succeeds would fire it again",
    );
    expect(screen.getByRole("heading", { name: "Edit schedule" })).toBeInTheDocument();
  });

  it("creates a schedule that waits for an event, posting that cadence", async () => {
    mountApi([], [{ id: "wf-sales", title: "Sales" }]);
    mountForm("wf-sales");
    let posted: { cadence?: string } | null = null;
    server.use(
      http.post(`${BASE}/api/schedules`, async ({ request }) => {
        posted = (await request.json()) as { cadence?: string };
        return HttpResponse.json({ schedule: scheduleFixture({ id: "sched-9" }) });
      }),
    );
    render(<SchedulesPage />);

    await screen.findByText("No schedules yet.");
    await userEvent.click(screen.getAllByRole("button", { name: "New schedule" })[0]);
    await userEvent.selectOptions(
      await screen.findByLabelText("Workflow"),
      screen.getByRole("option", { name: "Sales" }),
    );
    await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Event" }),
      "on index repo-docs quiet 30m",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(posted).toEqual(expect.objectContaining({ cadence: "on index repo-docs quiet 30m" })),
    );
  });
});
