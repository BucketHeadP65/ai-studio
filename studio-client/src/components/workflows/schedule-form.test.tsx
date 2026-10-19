import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { ScheduleForm } from "./schedule-form";

// The create-only route onto a schedule: a workflow (fixed or picked), its declared parameters,
// a cadence with a live preview, and the gate/auto-approve knobs — every request its mount can
// fire is mocked here (MSW errors on anything unhandled).

const BASE = "http://studio.test";

const CARD = {
  archived_at: null,
  shape: { steps: 0, person_gates: 0, agent_gates: 0 },
  runs: { last: null },
  id: "wf-sales",
  title: "Fetch sales",
  summary: "",
  provisional: false,
  score: 1,
  trust: { structural_grades: 0, structural_mean: null, structural_last: null, approved_by: [], level: "machine-confirmed", runs_with_evidence: 1, failed_runs: 0 },
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
    trust: { structural_grades: 0, structural_mean: null, structural_last: null, approved_by: [], level: "machine-confirmed", runs_with_evidence: 1, failed_runs: 0 },
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
    http.post(`${BASE}/api/schedules/preview`, () =>
      HttpResponse.json({
        next: [
          "2026-08-24T00:00:00+00:00",
          "2026-08-24T00:30:00+00:00",
          "2026-08-24T01:00:00+00:00",
        ],
      }),
    ),
  );
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
