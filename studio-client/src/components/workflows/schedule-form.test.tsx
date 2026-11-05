import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { Schedule } from "@/api/schedules";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { ScheduleForm } from "./schedule-form";

// The route onto a new schedule and onto a change to one: a workflow (fixed or picked, or the
// edited schedule's own), its declared parameters, a cadence with a live preview, and the gate
// mode options. Every request its mount can fire is mocked here (MSW errors on anything
// unhandled).

const BASE = "http://studio.test";
const KNOWLEDGE = "http://api.test";

const CARD = {
  archived_at: null,
  shape: { steps: 0, person_gates: 0, agent_gates: 0 },
  runs: { last: null },
  id: "wf-sales",
  title: "Fetch sales",
  summary: "",
  provisional: false,
  score: 1,
  trust: {
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    approved_by: [],
    level: "machine-confirmed",
    runs_with_evidence: 1,
    failed_runs: 0,
    total: {
      runs_with_evidence: 1,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    versions: [
      {
        version: 1,
        level: "machine-confirmed",
        runs_with_evidence: 1,
        failed_runs: 0,
        approved_by: [],
        structural_grades: 0,
        structural_mean: null,
        structural_last: null,
      },
    ],
  },
  needs: [],
  missing: [],
  brings: [],
  version: 1,
};

function workflowFixture() {
  return {
    title: "",
    summary: "",
    applicability: "",
    archived_at: null,
    ok: true,
    id: "wf-sales",
    name: "wf-sales",
    version: 1,
    description: "fetch sales",
    provisional: false,
    graph: {
      name: "wf-sales",
      version: 1,
      nodes: [
        {
          key: "param:quarter",
          id: "quarter",
          kind: "param",
          signature: "string",
          detail: "the fiscal quarter",
          tools: [],
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "string", minLength: 1 },
          source: "",
        },
        {
          key: "param:dry_run",
          id: "dry_run",
          kind: "param",
          signature: "boolean",
          required: false,
          detail: "",
          tools: [],
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "boolean" },
          source: "",
        },
      ],
      edges: [],
    },
    issues: [],
    fits: true,
    missing: [],
    brings: [],
    spec_yaml: "name: wf-sales\n",
    staleness: [],
    lineage: null,
    birth: null,
    carried: null,
    predictions: null,
    prediction_record: null,
    trust: {
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
      approved_by: [],
      level: "machine-confirmed",
      runs_with_evidence: 1,
      failed_runs: 0,
      total: {
        runs_with_evidence: 1,
        failed_runs: 0,
        approved_by: [],
        structural_grades: 0,
        structural_mean: null,
        structural_last: null,
      },
      versions: [
        {
          version: 1,
          level: "machine-confirmed",
          runs_with_evidence: 1,
          failed_runs: 0,
          approved_by: [],
          structural_grades: 0,
          structural_mean: null,
          structural_last: null,
        },
      ],
    },
  };
}

// A workflow that also asks which knowledge bases to work over.
function knowledgeBaseWorkflowFixture() {
  const plain = workflowFixture();
  return {
    ...plain,
    id: "wf-digest",
    name: "wf-digest",
    graph: {
      ...plain.graph,
      nodes: [
        ...plain.graph.nodes,
        {
          key: "param:knowledge_bases",
          id: "knowledge_bases",
          kind: "param",
          signature: "array",
          detail: "",
          tools: [],
          required: false,
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "array", items: { type: "string" } },
          source: "",
        },
      ],
    },
    presentation: {
      purpose: "",
      produces: "",
      owner_participation: "",
      not_for: "",
      inputs: [{ key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" }],
      result_fields: [],
      actions: [],
    },
  };
}

// A second workflow asking the very same questions: two schedules, two drafts.
const OTHER_CARD = { ...CARD, id: "wf-costs", title: "Fetch costs" };

function mountApi(): void {
  server.use(
    http.get(`${BASE}/api/library`, () => HttpResponse.json([CARD, OTHER_CARD])),
    http.get(`${BASE}/api/workflow/wf-sales`, () => HttpResponse.json(workflowFixture())),
    http.get(`${BASE}/api/workflow/wf-costs`, () =>
      HttpResponse.json({ ...workflowFixture(), id: "wf-costs", name: "wf-costs" }),
    ),
    http.get(`${BASE}/api/workflow/wf-digest`, () =>
      HttpResponse.json(knowledgeBaseWorkflowFixture()),
    ),
    http.get(`${KNOWLEDGE}/api/knowledge-bases`, () =>
      HttpResponse.json({
        knowledge_bases: ["research", "repo-docs"].map((name) => ({
          name,
          registered: true,
          indexed: true,
          broken: false,
          path: `/kb/${name}`,
          description: "",
          pages: 1,
          page_types: [],
          last_indexed: null,
        })),
      }),
    ),
    // An event cadence has no set times, so the preview answers none for it.
    http.post(`${BASE}/api/schedules/preview`, async ({ request }) => {
      const { cadence } = (await request.json()) as { cadence: string };
      if (/^(on |after )/.test(cadence)) return HttpResponse.json({ next: [] });
      return HttpResponse.json({
        next: [
          "2026-08-24T00:00:00+00:00",
          "2026-08-24T00:30:00+00:00",
          "2026-08-24T01:00:00+00:00",
        ],
      });
    }),
  );
}

function scheduleFixture(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sched-1",
    workflow_id: "wf-sales",
    arguments: { quarter: "Q1" },
    cadence: "every 30m",
    enabled: true,
    gate_policy: "wait",
    gate_mode: "wait",
    capacity_key: null,
    capacity_limit: null,
    blackout: null,
    catch_up: false,
    session_id: "",
    created_at: "2026-08-23T10:00:00+00:00",
    last_run_at: null,
    last_outcome: "",
    next_run_at: "2026-08-23T10:30:00+00:00",
    live: null,
    paused_at: null,
    spend_today_usd: "0",
    spend_week_usd: "0",
    spend_today_unpriced: 0,
    spend_week_unpriced: 0,
    ...overrides,
  };
}

function renderEdit(overrides: Partial<Schedule> = {}) {
  const onSave = vi.fn();
  render(
    <ScheduleForm
      open
      schedule={scheduleFixture(overrides)}
      submitting={false}
      onSave={onSave}
      onCancel={vi.fn()}
    />,
  );
  return { onSave };
}

describe("ScheduleForm", () => {
  it("renders nothing when closed", () => {
    render(
      <ScheduleForm
        open={false}
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the preselected workflow fixed, fetches its parameter, and previews the default cadence", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("wf-sales")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Workflow" })).toBeNull();
    expect(await screen.findByLabelText("quarter")).toBeInTheDocument();
    expect(await screen.findByText(/Next three fires/)).toBeInTheDocument();
  });

  it("offers a workflow select fed by the library when opened standalone", async () => {
    mountApi();
    render(<ScheduleForm open submitting={false} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    const select = await screen.findByRole("combobox", { name: "Workflow" });
    await screen.findByRole("option", { name: "Fetch sales" });
    await userEvent.selectOptions(select, "wf-sales");

    expect(await screen.findByLabelText("quarter")).toBeInTheDocument();
  });

  it("starts a blank draft on another workflow, even one already read and asking the same questions", async () => {
    mountApi();
    render(<ScheduleForm open submitting={false} onSubmit={vi.fn()} onCancel={vi.fn()} />);

    const select = await screen.findByRole("combobox", { name: "Workflow" });
    await screen.findByRole("option", { name: "Fetch sales" });
    await userEvent.selectOptions(select, "wf-sales");
    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");

    await userEvent.selectOptions(select, "wf-costs");
    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue(""));
    await userEvent.type(screen.getByLabelText("quarter"), "Q2");

    // Back to a workflow already read once, so its questions arrive with no gap to clear them.
    await userEvent.selectOptions(select, "wf-sales");

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue(""));
  });

  it("asks for the arguments first and the cadence after them", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const argumentsLegend = await screen.findByText("Arguments");
    const cadenceLegend = screen.getByText("Cadence");
    expect(
      argumentsLegend.compareDocumentPosition(cadenceLegend) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("creates nothing while a declared parameter is wrong, saying which one instead", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByLabelText("quarter");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(screen.getByText("This is required.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("1 field needs an answer.");
    expect(onSubmit).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("asks for arguments through the same field renderer a run is set up with", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByLabelText("dry_run"));
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    // A boolean parameter rides as a boolean, never as the word for one.
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ arguments: { quarter: "Q1", dry_run: true } }),
    );
  });

  it("submits the composed interval cadence, arguments, gate policy, and gate mode", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(
      screen.getByRole("radio", { name: "Pass automatically (recorded as automatic)" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledWith({
      workflowId: "wf-sales",
      arguments: { quarter: "Q1", dry_run: false },
      cadence: "every 30m",
      gatePolicy: "wait",
      gateMode: "auto",
      capacityKey: null,
      capacityLimit: null,
      blackout: null,
      catchUp: false,
    });
  });

  it("defaults gate mode to wait and posts nothing more when never touched", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("radio", { name: "Wait for me" })).toBeChecked();
    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ gateMode: "wait" }));
  });

  it("picking the orchestrator decides posts gate_mode orchestrate", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByRole("radio", { name: "The orchestrator decides" }));
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ gateMode: "orchestrate" }));
  });

  it("keeps the gate mode options reachable by keyboard, tabbing onto a radio named for its own sentence", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    // Selecting "Fail after" also enables its own seconds field, so it is the next stop in the
    // tab order before the first gate mode option.
    await userEvent.click(screen.getByRole("radio", { name: /Fail after/ }));
    await userEvent.tab();
    expect(screen.getByLabelText("Seconds before failing")).toHaveFocus();

    await userEvent.tab();
    expect(screen.getByRole("radio", { name: "Wait for me" })).toHaveFocus();
  });

  it("keeps the advanced section collapsed until opened", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const capacityKeyInput = screen.getByLabelText("Capacity key");
    expect(capacityKeyInput).not.toBeVisible();

    await userEvent.click(screen.getByText("Advanced"));
    expect(capacityKeyInput).toBeVisible();
  });

  it("enables the capacity limit only once a capacity key is set", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText("Advanced"));
    const capacityLimit = screen.getByLabelText("Capacity limit");
    expect(capacityLimit).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Capacity key"), "gpu-pool");
    expect(capacityLimit).toBeEnabled();
  });

  it("refuses a capacity key with no limit, saying so beside the field", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.type(screen.getByLabelText("Capacity key"), "gpu-pool");

    expect(screen.getByLabelText("Capacity limit")).toHaveAccessibleDescription(
      "Give the capacity a limit, or leave the key empty.",
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText("Capacity limit"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ capacityKey: "gpu-pool", capacityLimit: 2 }),
    );
  });

  it("refuses a capacity limit that is not a whole number when creating", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.type(screen.getByLabelText("Capacity key"), "gpu-pool");
    await userEvent.type(screen.getByLabelText("Capacity limit"), "3x");

    expect(screen.getByLabelText("Capacity limit")).toHaveAccessibleDescription(
      "The capacity limit must be a whole number above zero.",
    );
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("nudges softly when the blackout text does not look like the expected format, without blocking submission", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByText("Advanced"));
    expect(screen.queryByText(/does not look like a blackout window/)).toBeNull();

    await userEvent.type(screen.getByLabelText("Blackout windows"), "whenever");
    expect(screen.getByText(/does not look like a blackout window/)).toBeInTheDocument();

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    expect(screen.getByRole("button", { name: "Create" })).not.toBeDisabled();
  });

  it("collects the advanced fields and round-trips them to onSubmit", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.type(screen.getByLabelText("Capacity key"), "gpu-pool");
    await userEvent.type(screen.getByLabelText("Capacity limit"), "3");
    await userEvent.type(screen.getByLabelText("Blackout windows"), "22:00-06:00");
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Catch up a missed fire when possible" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        capacityKey: "gpu-pool",
        capacityLimit: 3,
        blackout: "22:00-06:00",
        catchUp: true,
      }),
    );
  });

  it("shows the backend's blackout refusal sentence verbatim when creation is refused", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        error="'whenever' is not a blackout -- a blackout looks like one or more 'HH:MM-HH:MM' windows, comma-separated (e.g. '22:00-06:00', wrapping midnight allowed)"
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "'whenever' is not a blackout -- a blackout looks like one or more 'HH:MM-HH:MM' windows, comma-separated (e.g. '22:00-06:00', wrapping midnight allowed)",
    );
  });

  it("switches to a cron expression and previews it instead of the interval", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("radio", { name: "Cron expression" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Cron expression" }), "0 9 * * 1");

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Cron expression" })).toHaveValue("0 9 * * 1"),
    );
    expect(await screen.findByText(/Next three fires/)).toBeInTheDocument();
  });

  it("shows the backend's cadence sentence verbatim on a bad cron expression", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/schedules/preview`, () =>
        HttpResponse.json(
          { ok: false, message: "'whenever' is not a cadence -- a cadence looks like ..." },
          { status: 400 },
        ),
      ),
    );
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("radio", { name: "Cron expression" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Cron expression" }), "whenever");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "'whenever' is not a cadence -- a cadence looks like ...",
    );
  });

  it("uses only the house's allowed punctuation on the gates-on-this-schedule choice", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;

    const legend = await screen.findByText("Gates on runs from this schedule");
    expect(legend.textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
    for (const name of [
      "Wait for me",
      "The orchestrator decides",
      "Pass automatically (recorded as automatic)",
    ]) {
      expect(screen.getByRole("radio", { name }).parentElement?.textContent ?? "").toMatch(
        ALLOWED_PUNCTUATION,
      );
    }
  });

  it("cancels without submitting", async () => {
    mountApi();
    const onCancel = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={onCancel}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("creates a schedule that waits for an event, saying it has no set times", async () => {
    mountApi();
    const onSubmit = vi.fn();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(await screen.findByLabelText("quarter"), "Q1");
    await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
    expect(
      screen.getByText(/runs after a refresh brings new sources into a knowledge base/),
    ).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Event" }), "after research-digest");

    expect(
      await screen.findByText("Waits for that event instead of running at set times."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ cadence: "after research-digest" }),
    );
  });

  it.each(["on refresh", "after research-digest"])(
    "warns that %s never runs a workflow that asks for no knowledge base",
    async (cadence) => {
      mountApi();
      render(
        <ScheduleForm
          open
          workflowId="wf-sales"
          submitting={false}
          onSubmit={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

      await screen.findByLabelText("quarter");
      await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
      await userEvent.type(screen.getByRole("textbox", { name: "Event" }), cadence);

      expect(
        screen.getByText(
          "This workflow does not ask for a knowledge base, so this event will never run it.",
        ),
      ).toBeInTheDocument();
    },
  );

  it("explains the on step change event and does not warn that it needs a knowledge base", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByLabelText("quarter");
    await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
    expect(
      screen.getByText(/runs once new reusable step versions are imported and 10 minutes pass/),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByRole("textbox", { name: "Event" }),
      "on step change quiet 10m",
    );

    expect(screen.queryByText(/does not ask for a knowledge base/)).toBeNull();
  });

  it("does not warn for an on index event, which names its own knowledge base", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByLabelText("quarter");
    await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Event" }), "on index repo-docs");

    expect(screen.queryByText(/does not ask for a knowledge base/)).toBeNull();
  });

  it("does not warn when the workflow asks for a knowledge base", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-digest"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByLabelText("quarter");
    expect(screen.getByText("Knowledge bases")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: /On an event/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Event" }), "on refresh");

    expect(screen.queryByText(/does not ask for a knowledge base/)).toBeNull();
  });

  it("shows the create mutation's own error", async () => {
    mountApi();
    render(
      <ScheduleForm
        open
        workflowId="wf-sales"
        submitting={false}
        error="this app cannot run it"
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("this app cannot run it");
  });
});

describe("ScheduleForm, editing a schedule", () => {
  it("opens on the schedule's own values, titled as an edit, with its workflow fixed", async () => {
    mountApi();
    renderEdit({
      gate_policy: "fail_after:120",
      gate_mode: "auto",
      capacity_key: "gpu-pool",
      capacity_limit: 3,
      blackout: "22:00-06:00",
      catch_up: true,
    });

    expect(screen.getByRole("heading", { name: "Edit schedule" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
    expect(screen.getByText("wf-sales")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Workflow" })).toBeNull();
    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    expect(screen.getByRole("radio", { name: /Fail after/ })).toBeChecked();
    expect(screen.getByLabelText("Seconds before failing")).toHaveValue("120");
    expect(
      screen.getByRole("radio", { name: "Pass automatically (recorded as automatic)" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Capacity key")).toHaveValue("gpu-pool");
    expect(screen.getByLabelText("Capacity limit")).toHaveValue("3");
    expect(screen.getByLabelText("Blackout windows")).toHaveValue("22:00-06:00");
    expect(
      screen.getByRole("checkbox", { name: "Catch up a missed fire when possible" }),
    ).toBeChecked();
  });

  it("splits an interval into its count and unit", async () => {
    mountApi();
    renderEdit({ cadence: "every 2h" });

    expect(screen.getByRole("radio", { name: /^Every/ })).toBeChecked();
    expect(screen.getByLabelText("Interval count")).toHaveValue("2");
    expect(screen.getByLabelText("Interval unit")).toHaveValue("h");
    expect(await screen.findByText(/Next three fires/)).toBeInTheDocument();
  });

  it("puts a cron expression in the cron box", async () => {
    mountApi();
    renderEdit({ cadence: "0 9 * * 1" });

    expect(screen.getByRole("radio", { name: /Cron expression/ })).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Cron expression" })).toHaveValue("0 9 * * 1");
  });

  it.each([
    "on refresh",
    "after research-digest",
    "on index repo-docs quiet 30m",
    "on step change",
    "on step change quiet 10m",
  ])(
    "puts the event cadence %s in the event box",
    async (cadence) => {
      mountApi();
      renderEdit({ cadence });

      expect(screen.getByRole("radio", { name: /On an event/ })).toBeChecked();
      expect(screen.getByRole("textbox", { name: "Event" })).toHaveValue(cadence);
      expect(screen.queryByText(/kept here as written/)).toBeNull();
      expect(
        await screen.findByText("Waits for that event instead of running at set times."),
      ).toBeInTheDocument();
    },
  );

  it("keeps an interval the unit select cannot show in the event box as written", async () => {
    mountApi();
    renderEdit({ cadence: "every 45s" });

    expect(screen.getByRole("radio", { name: /On an event/ })).toBeChecked();
    expect(screen.getByRole("textbox", { name: "Event" })).toHaveValue("every 45s");
    expect(
      screen.getByText("The other fields cannot show this cadence, so it is kept here as written."),
    ).toBeInTheDocument();
  });

  it("keeps Save changes off until a field changes, then hands back only that field", async () => {
    mountApi();
    const { onSave } = renderEdit();

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();

    await userEvent.click(screen.getByRole("radio", { name: "The orchestrator decides" }));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith({ gate_mode: "orchestrate" });
  });

  it("sends every argument once one of them changes", async () => {
    mountApi();
    const { onSave } = renderEdit();

    const quarter = await screen.findByLabelText("quarter");
    await waitFor(() => expect(quarter).toHaveValue("Q1"));
    await userEvent.clear(quarter);
    await userEvent.type(quarter, "Q2");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith({ arguments: { quarter: "Q2", dry_run: false } });
  });

  it("sends no cadence it only rewrote while opening", async () => {
    mountApi();
    const { onSave } = renderEdit({ cadence: "EVERY 30M" });

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    expect(screen.getByLabelText("Interval unit")).toHaveValue("m");
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Catch up a missed fire when possible" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith({ catch_up: true });
  });

  it("clears an emptied blackout by sending null", async () => {
    mountApi();
    const { onSave } = renderEdit({ blackout: "22:00-06:00" });

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.clear(screen.getByLabelText("Blackout windows"));
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith({ blackout: null });
  });

  it("refuses a capacity limit that is not a whole number, saying so beside the field", async () => {
    mountApi();
    const { onSave } = renderEdit({ capacity_key: "gpu-pool", capacity_limit: 3 });

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.type(screen.getByLabelText("Capacity limit"), "x");

    expect(screen.getByLabelText("Capacity limit")).toHaveValue("3x");
    expect(screen.getByLabelText("Capacity limit")).toHaveAccessibleDescription(
      "The capacity limit must be a whole number above zero.",
    );
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).not.toHaveBeenCalled();
  });

  it("says an emptied limit clears the capacity key too, and sends both cleared", async () => {
    mountApi();
    const { onSave } = renderEdit({ capacity_key: "gpu-pool", capacity_limit: 3 });

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.clear(screen.getByLabelText("Capacity limit"));

    expect(screen.getByLabelText("Capacity limit")).toHaveAccessibleDescription(
      "With no limit, saving clears the capacity key too.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({ capacity_key: null, capacity_limit: null });
  });

  it("clears an emptied capacity key, and the limit that goes with it, by sending null", async () => {
    mountApi();
    const { onSave } = renderEdit({ capacity_key: "gpu-pool", capacity_limit: 3 });

    await waitFor(() => expect(screen.getByLabelText("quarter")).toHaveValue("Q1"));
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.clear(screen.getByLabelText("Capacity key"));
    expect(screen.queryByText(/saving clears the capacity key too/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect(onSave).toHaveBeenCalledWith({ capacity_key: null, capacity_limit: null });
  });
});
