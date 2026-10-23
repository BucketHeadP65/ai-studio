import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "@/api/sessions-types";
import type { RunHistoryRecord, RunState, RunValues, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

// The page owns four things a run's history changes: what the outcome strip and the gate
// trail report, what the canvas draws, what the timeline shows below it, and what a clicked
// node's drawer reads. `null` names the live row throughout; picking a past run swaps all four
// to that run's own archived record, and never touches the live session's own polls -- the
// control strip keeps acting on the live session regardless of which run the body is currently
// showing. The selected run also lives on the URL's own `?run=` search param, read once at
// mount and written back on every later selection.

let searchParamsValue = new URLSearchParams();
const routerReplace = vi.fn();
const routerPush = vi.fn();

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf.lit-check.v5" }),
  useRouter: () => ({ back: vi.fn(), replace: routerReplace, push: routerPush }),
  useSearchParams: () => searchParamsValue,
}));

afterEach(() => {
  searchParamsValue = new URLSearchParams();
  routerReplace.mockClear();
  routerPush.mockClear();
});

// Imported after the mock so the page sees the mocked module.
import WorkflowPage from "./page";

// The canvas card starts folded, so every test that reads the drawn graph opens it first.
async function openCanvas(): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: "Canvas" }));
}

const BASE = "http://studio.test";

const DETAIL: WorkflowDetail = {
  ok: true,
  id: "wf.lit-check.v5",
  archived_at: null,
  name: "lit-check",
  version: 5,
  description: "Check a decision against the literature.",
  title: "Check against the literature",
  summary: "Checks one decision against the literature.",
  applicability: "Use before a decision ships. Do not use for a broad survey.",
  provisional: false,
  graph: {
    name: "lit-check",
    version: 5,
    nodes: [
      {
        key: "draft",
        id: "draft",
        kind: "run",
        signature: "compose(context: string) → Record",
        detail: "",
        tools: [],
        required: true,
        is_output: true,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
    ],
    edges: [],
  },
  issues: [],
  fits: true,
  missing: [],
  brings: [],
  spec_yaml: "name: lit-check\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
  trust: {
    level: "machine-confirmed",
    runs_with_evidence: 2,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
};

const IDLE: RunState = {
  running: false,
  workflow: null,
  result: null,
  active: [],
  done: [],
  failed: [],
  waiting_at: null,
  events: [],
  seq: 0,
  auto_approve: false,
  gate_mode: "wait",
};

const LIVE_VALUES: RunValues = { workflow: DETAIL.id, values: { draft: "live output" } };

// SpendReport mounts under OutcomeStrip whenever a run is in view, so every test that reaches
// that state needs this handler regardless of what it is actually asserting about spend.
const SPEND_ANSWER = {
  available: true,
  scope: "step",
  subject: "run-1",
  since_hours: 720,
  lines: [],
  total: {
    key: "total",
    calls: 0,
    cost_usd: "0",
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  },
  source: "telemetry",
};

const DRAFT_REF = {
  id: "draft",
  label: "The draft",
  run_id: "run-42",
  node_id: "archived-step",
  turn: null,
  value_digest: "a".repeat(64),
  media_type: "text/markdown",
} as const;

const PAST_RECORD: RunHistoryRecord = {
  spend: null,
  run_id: "run-42",
  run_title: "Which cache do we use?",
  workflow_id: DETAIL.id,
  outcome: "completed",
  result: {
    status: "succeeded",
    summary: "it published",
    attempted: 1,
    succeeded: 1,
    skipped: 0,
    failed: 0,
    retry_attempts: 0,
    primary_input: 1,
    generation: 0,
    checks: [],
    artifacts: [DRAFT_REF],
  },
  failure: "",
  started_at: "2026-08-20T10:00:00Z",
  ended_at: "2026-08-20T10:05:00Z",
  runner: "app:chat",
  session_id: "sess-old",
  params: {},
  gates_approved: ["review-gate"],
  gates_rejected: {},
  gates_auto: [],
  gates_orchestrated: [],
  pinned: false,
  spec: {},
  values: { "archived-step": "archived output" },
  conversations: {
    // A judge's own line, keyed the real way the archive writes it: the graph node id, always
    // bracketed, even for the one line a plain step carries.
    "judge-with-line[]": [
      { kind: "response", parts: [{ part_kind: "text", content: "Approved on review." }] },
    ],
  },
  owner_reviews: [],
  events: [
    { seq: 1, node: "archived-step", at: 1.2, finished: true, failed: false },
    { seq: 2, node: "worker-step", at: 1.4, finished: true, failed: false },
    { seq: 3, node: "review-gate", at: 2, finished: true, failed: false },
  ],
  graph: {
    name: "lit-check",
    version: 4,
    nodes: [
      {
        key: "archived-step",
        id: "archived-step",
        kind: "run",
        signature: "old_step() → str",
        detail: "",
        tools: [],
        required: true,
        is_output: true,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
        work: "archived_step_name",
      },
      {
        key: "worker-step",
        id: "worker-step",
        kind: "run",
        signature: "worker_step() → str",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
        work: "worker_step_name",
      },
      {
        key: "review-gate",
        id: "review-gate",
        kind: "gate",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
      // A judge's own briefing is always blank on the rendered graph (the backend blanks it),
      // so a judge can only be told agentic by an actual stored line -- one of these carries
      // one, the other does not.
      {
        key: "judge-with-line",
        id: "judge-with-line",
        kind: "judge",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: true,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
      {
        key: "judge-no-line",
        id: "judge-no-line",
        kind: "judge",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: true,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
    ],
    edges: [],
  },
};

/** The session a new version's draft is opened in, as the sessions API answers a fresh one. */
const DRAFT_SESSION: SessionSummary = {
  id: "sess-draft",
  project_id: "proj-1",
  title: "session sess-draft",
  started_at: "2026-08-20T10:00:00Z",
  last_activity_at: "2026-08-20T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: 0,
  usage: { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 },
  kind: "chat",
  cost_usd: "0",
  unpriced: 0,
  context: null,
};

function mountApi() {
  let historyRuns: unknown[] = [
    {
      run_title: "",
      gates_orchestrated: [],
      spend: null,
      result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
      run_id: "sess-1",
      workflow_id: DETAIL.id,
      outcome: "live",
      failure: "",
      started_at: "2026-08-20T09:00:00Z",
      ended_at: "",
      runner: "",
      session_id: "sess-1",
      params: {},
      gates_approved: [],
      gates_rejected: {},
      gates_auto: [],
      pinned: false,
    },
    PAST_RECORD,
  ];

  server.use(
    http.get(`${BASE}/api/workflow/${DETAIL.id}`, () => HttpResponse.json(DETAIL)),
    http.get(`${BASE}/api/runs`, () =>
      HttpResponse.json({
        runs: [
          {
            run_title: "",
            started_at: "",
            session_id: "sess-1",
            workflow_id: DETAIL.id,
            state: "running",
            active: ["draft"],
            done: [],
            failed: [],
            waiting_at: null,
            gate: null,
          },
        ],
      }),
    ),
    http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
      HttpResponse.json({ runs: historyRuns }),
    ),
    http.get(`${BASE}/api/runs/history/run-42`, () => HttpResponse.json(PAST_RECORD)),
    http.get(`${BASE}/api/sessions/sess-1/run/values`, () => HttpResponse.json(LIVE_VALUES)),
    http.get(`${BASE}/api/sessions/sess-1/run/state`, () =>
      HttpResponse.json({ ...IDLE, running: true, workflow: DETAIL.id }),
    ),
    http.patch(`${BASE}/api/runs/history/run-42`, async ({ request }) => {
      const body = (await request.json()) as { pinned: boolean };
      historyRuns = historyRuns.map((run) =>
        (run as { run_id: string }).run_id === "run-42"
          ? { ...PAST_RECORD, pinned: body.pinned }
          : run,
      );
      return HttpResponse.json({ ok: true, pinned: body.pinned });
    }),
    http.delete(`${BASE}/api/runs/history/run-42`, () => {
      historyRuns = historyRuns.filter((run) => (run as { run_id: string }).run_id !== "run-42");
      return HttpResponse.json({ ok: true });
    }),
    http.get(`${BASE}/api/spend`, () => HttpResponse.json(SPEND_ANSWER)),
  );
}

describe("WorkflowPage", () => {
  it("shows the live row selected by default, drawing the live graph and node output", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    expect(await screen.findByText("Live")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();

    // A bare click, not the full press sequence: React Flow's drag handling
    // lives on mousedown and needs real layout jsdom does not have.
    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="draft"]');
      if (!found) throw new Error("draft card not drawn yet");
      return found;
    });
    fireEvent.click(card);
    // The values panel already previews this same text compactly in the aside, so the full
    // reading is scoped to the drawer it opened rather than to the page as a whole.
    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("live output")).toBeInTheDocument();
  });

  it("selecting a past run repaints the canvas, the timeline, and the node drawer from its own record", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="archived-step"]');
      if (!found) throw new Error("archived-step card not drawn yet");
      return found;
    });
    fireEvent.click(card);
    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("archived output")).toBeInTheDocument();
  });

  it("after selecting a past run, the outcome strip, the gate trail, the canvas, the timeline, and the rail all show together", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));
    await openCanvas();
    await waitFor(() => {
      if (!container.querySelector('[data-node="archived-step"]')) {
        throw new Error("graph not drawn yet");
      }
    });

    // the outcome strip and the timeline header both read the archived record's own duration
    expect(screen.getAllByText("5m 0s")).toHaveLength(2);
    // the gate trail, reading the archived record's single decided gate honestly
    expect(screen.getByText("Approved by a person")).toBeInTheDocument();
    const decisions = screen.getByText("Decisions").nextElementSibling;
    expect(decisions).toHaveTextContent("1");
    expect(decisions).toHaveClass("font-mono");
    // the time chart, in its own card below
    expect(screen.getByTitle("archived-step")).toBeInTheDocument();
    // the history rail, still reachable beside it
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows the newest archived record when nothing is live and nothing is explicitly selected", async () => {
    const OLDER_RECORD: RunHistoryRecord = {
      ...PAST_RECORD,
      run_id: "run-10",
      outcome: "failed",
      started_at: "2026-08-19T10:00:00Z",
      ended_at: "2026-08-19T10:10:00Z",
    };
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () => HttpResponse.json(DETAIL)),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
        // `workflowHistory` answers newest first; PAST_RECORD (run-42) is the newest here.
        HttpResponse.json({ runs: [PAST_RECORD, OLDER_RECORD] }),
      ),
      http.get(`${BASE}/api/runs/history/run-42`, () => HttpResponse.json(PAST_RECORD)),
      http.get(`${BASE}/api/runs/history/run-10`, () => HttpResponse.json(OLDER_RECORD)),
      http.get(`${BASE}/api/spend`, () => HttpResponse.json(SPEND_ANSWER)),
    );
    const { container } = render(<WorkflowPage />);

    await openCanvas();
    await waitFor(() => {
      if (!container.querySelector('[data-node="archived-step"]')) {
        throw new Error("graph not drawn yet");
      }
    });

    expect(
      screen.queryByText("No run yet. Run it from the panel on the right."),
    ).not.toBeInTheDocument();
    // PAST_RECORD's own 5-minute duration, not OLDER_RECORD's 10-minute one, proves the newest
    // record is the one in view -- read off both the outcome strip and the timeline header.
    expect(screen.getAllByText("5m 0s")).toHaveLength(2);
    expect(screen.queryByText("10m 0s")).not.toBeInTheDocument();
  });

  it("shows just the canvas and an empty sentence for a workflow that has never run", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () => HttpResponse.json(DETAIL)),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () => HttpResponse.json({ runs: [] })),
    );
    const { container } = render(<WorkflowPage />);

    expect(
      await screen.findByText("No run yet. Run it from the panel on the right."),
    ).toBeInTheDocument();
    await openCanvas();
    await waitFor(() => {
      if (!container.querySelector('[data-node="draft"]')) throw new Error("canvas not drawn yet");
    });
  });

  it("keeps the canvas card folded until it is asked for, then reopens and closes on the same control", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    const toggle = await screen.findByRole("button", { name: "Canvas" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelector('[data-node="draft"]')).not.toBeInTheDocument();

    await userEvent.click(toggle);
    await waitFor(() => {
      if (!container.querySelector('[data-node="draft"]')) throw new Error("the canvas never drew");
    });

    await userEvent.click(toggle);
    expect(container.querySelector('[data-node="draft"]')).not.toBeInTheDocument();
  });

  it("places the canvas card above the gate trail, tall enough to read a graph", async () => {
    mountApi();
    render(<WorkflowPage />);

    // The archived record's own graph carries a gate node, so the gate trail renders here.
    await userEvent.click(await screen.findByText("Completed"));

    const canvasToggle = await screen.findByRole("button", { name: "Canvas" });
    const canvasCard = canvasToggle.closest("div");
    const gateTrailHeading = await screen.findByText("Gate trail");
    const position = canvasCard?.compareDocumentPosition(gateTrailHeading) ?? 0;
    const canvasComesFirst = (position & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    expect(canvasComesFirst).toBe(true);

    await userEvent.click(canvasToggle);
    const canvasFrame = canvasToggle.nextElementSibling;
    expect(canvasFrame).toHaveClass("min-h-[70vh]");
  });

  it("?run= on the URL preselects that archived run without a click", async () => {
    mountApi();
    searchParamsValue = new URLSearchParams("run=run-42");
    const { container } = render(<WorkflowPage />);

    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="archived-step"]');
      if (!found) throw new Error("archived-step card not drawn yet");
      return found;
    });
    fireEvent.click(card);
    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("archived output")).toBeInTheDocument();
  });

  it("selecting a run writes its id onto the URL", async () => {
    mountApi();
    render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    expect(routerReplace).toHaveBeenCalledWith("/workflows/wf.lit-check.v5?run=run-42");
  });

  it("keeps the plain text viewer for a step with no archived line, never the step chat", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="worker-step"]');
      if (!found) throw new Error("worker-step card not drawn yet");
      return found;
    });
    fireEvent.click(card);

    expect(await screen.findByText("No output recorded for this step.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  });

  it("tolerates a 404 for its own saved workflow, rendering the archived record's own graph and identity", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () =>
        HttpResponse.json({ detail: "no such workflow" }, { status: 404 }),
      ),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
        HttpResponse.json({ runs: [PAST_RECORD] }),
      ),
      http.get(`${BASE}/api/runs/history/run-42`, () => HttpResponse.json(PAST_RECORD)),
      http.get(`${BASE}/api/spend`, () => HttpResponse.json(SPEND_ANSWER)),
    );
    searchParamsValue = new URLSearchParams("run=run-42");

    const { container } = render(<WorkflowPage />);

    await openCanvas();
    await waitFor(() => {
      if (!container.querySelector('[data-node="archived-step"]')) {
        throw new Error("graph not drawn yet");
      }
    });

    // The record's own graph names the run now, not the (unreachable) saved workflow.
    expect(screen.getByRole("heading", { name: "lit-check v4" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the loading state, not the error, while its 404'd workflow's record is still in flight", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () =>
        HttpResponse.json({ detail: "no such workflow" }, { status: 404 }),
      ),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
        HttpResponse.json({ runs: [PAST_RECORD] }),
      ),
      // Never resolves -- the record stays pending for the whole test.
      http.get(`${BASE}/api/runs/history/run-42`, () => new Promise(() => {})),
    );
    searchParamsValue = new URLSearchParams("run=run-42");

    render(<WorkflowPage />);

    expect(await screen.findByText(`Loading ${DETAIL.id}.`)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the error when its 404'd workflow's own archived record fails to load too", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () =>
        HttpResponse.json({ detail: "no such workflow" }, { status: 404 }),
      ),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
        HttpResponse.json({ runs: [PAST_RECORD] }),
      ),
      http.get(`${BASE}/api/runs/history/run-42`, () =>
        HttpResponse.json({ detail: "no such run" }, { status: 404 }),
      ),
    );
    searchParamsValue = new URLSearchParams("run=run-42");

    render(<WorkflowPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(`Could not open ${DETAIL.id}.`);
  });

  it("opens the step chat for a judge that has a stored line, even though its rendered briefing is blank", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="judge-with-line"]');
      if (!found) throw new Error("judge-with-line card not drawn yet");
      return found;
    });
    fireEvent.click(card);

    expect(await screen.findByText("Approved on review.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  });

  it("keeps the plain text viewer for a judge with no stored line", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    await openCanvas();
    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="judge-no-line"]');
      if (!found) throw new Error("judge-no-line card not drawn yet");
      return found;
    });
    fireEvent.click(card);

    expect(await screen.findByText("No output recorded for this step.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  });

  it("pins a past run through the rail", async () => {
    mountApi();
    render(<WorkflowPage />);
    await screen.findByText("Completed");

    await userEvent.click(screen.getByRole("button", { name: "Pin this run" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Unpin this run" })).toBeInTheDocument(),
    );
  });

  it("deletes a past run after confirming, and the rail drops it", async () => {
    mountApi();
    render(<WorkflowPage />);
    await screen.findByText("Completed");

    await userEvent.click(screen.getByRole("button", { name: "Delete this run" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Delete this run?")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.queryByText("Completed")).not.toBeInTheDocument());
  });

  it("names the run it is showing by the run's own title", async () => {
    mountApi();
    render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    expect(await screen.findByText("Which cache do we use?")).toBeInTheDocument();
  });

  it("shows the checked result and the artifacts the run fixed above the folded canvas", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
    expect(screen.getByText("The draft")).toBeInTheDocument();
    const sections = Array.from(container.querySelectorAll("[data-section]")).map((section) =>
      section.getAttribute("data-section"),
    );
    expect(sections).toEqual([
      "result",
      "artifacts",
      "graph",
      "execution-evidence",
      "owner-review",
    ]);
    expect(screen.getByRole("button", { name: "Canvas" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("reads a completed run nobody reviewed as acceptance unknown, apart from what it did", async () => {
    mountApi();
    const { container } = render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));

    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
    const owner = container.querySelector('[data-section="owner-review"]');
    expect(owner).toHaveTextContent("Not reviewed");
    expect(owner).toHaveTextContent("whether it was any use is unknown");
    // Nowhere on the page, not only outside that block: the claim must not be able to move.
    expect(container).not.toHaveTextContent(/\bAccepted\b/);
    expect(container).not.toHaveTextContent(/\buseful\b/i);
  });

  it("hands the run it has open to the controls, so a new version cites that run", async () => {
    mountApi();
    let adapted: unknown = null;
    server.use(
      http.get(`${BASE}/api/workflows/${DETAIL.id}/history`, () =>
        HttpResponse.json({
          runs: [
            { ...PAST_RECORD, run_id: "run-7", run_title: "the newest run" },
            { ...PAST_RECORD, run_id: "run-2", run_title: "the run being read" },
          ],
        }),
      ),
      http.get(`${BASE}/api/runs/history/run-2`, () =>
        HttpResponse.json({ ...PAST_RECORD, run_id: "run-2", run_title: "the run being read" }),
      ),
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: DRAFT_SESSION }),
      ),
      http.post(`${BASE}/api/sessions/sess-draft/adapt`, async ({ request }) => {
        adapted = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    searchParamsValue = new URLSearchParams("run=run-2");
    render(<WorkflowPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Make a new version" }));
    const dialog = await screen.findByRole("dialog");
    const does = within(dialog).getByLabelText("What this version does");
    await userEvent.clear(does);
    await userEvent.type(does, "Check a decision, and name the sections it could not read.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Make the new version" }));

    await waitFor(() =>
      expect(adapted).toMatchObject({ workflow_id: DETAIL.id, source_run_id: "run-2" }),
    );
  });

  it("reads one artifact through the run's own resolve route when it is asked for", async () => {
    mountApi();
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/runs/history/run-42/artifacts/resolve`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json({ ref: DRAFT_REF, value: "net: 400" });
      }),
    );
    render(<WorkflowPage />);

    await userEvent.click(await screen.findByText("Completed"));
    await userEvent.click(await screen.findByRole("button", { name: "Show" }));

    expect(await screen.findByText("net: 400")).toBeInTheDocument();
    expect(sent).toEqual([{ ref: DRAFT_REF }]);
  });
});
