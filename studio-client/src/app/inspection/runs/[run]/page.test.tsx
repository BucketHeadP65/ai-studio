import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { duration, longDate } from "@/api/inspection-words";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

vi.mock("next/navigation", () => ({
  useParams: vi.fn(() => ({ run: "harness-author-d01d1796" })),
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the page sees the mocked module.
import { useParams } from "next/navigation";

import RunOverviewPage from "./page";

const BASE = "http://studio.test";
const RUN_ID = "harness-author-d01d1796";
const SOURCE = `record:${RUN_ID}`;

const LEAD_SETTLED = {
  lead_id: 1,
  source: SOURCE,
  watch: "every-run",
  symptom: "warning_ignored",
  spans: ["5.8.0", "5.9.0"],
  found_by: "warning_ignored",
  seen_at: "2026-09-10T12:06:00+00:00",
  attempted_at: null,
  refusal: null,
  resolution: "open",
};

const LEAD_DISMISSED = {
  lead_id: 2,
  source: SOURCE,
  watch: "every-run",
  symptom: "retried_after_error",
  spans: ["3.4.0", "3.6.0"],
  found_by: "retried_after_error",
  seen_at: "2026-09-10T12:07:00+00:00",
  attempted_at: "2026-09-10T12:07:30+00:00",
  refusal: "the retry at 3.6.0 changed the arguments after the error at 3.4.0",
  resolution: "dismissed",
};

const FINDING_1 = {
  finding_id: 1,
  lead_id: 1,
  question_id: 7,
  source: SOURCE,
  symptom: "warning_ignored",
  symptom_text: "the transcript shows a warning the agent never acted on",
  explanation: "The agent read a stale warning as a clean confirmation and ran the workflow anyway.",
  found_by: "inspector",
  model: "z-ai/glm-5.3-flash",
  decided_at: "2026-09-10T12:17:00+00:00",
  blames: [
    {
      root_cause: "agent_reasoning",
      root_cause_text: "the agent erred though it had what it needed",
      surface_kind: "model_tier",
      surface_name: "",
      why: "the return at 5.8.0 said 're-check before trusting it'",
    },
    {
      root_cause: "tool_result_shape",
      root_cause_text: "a tool's own return mixed the warning with a confirmation",
      surface_kind: "tool",
      surface_name: "fetch_workflow_spec",
      why: "the return at 5.8.0 shows the tool answered oddly",
    },
  ],
  lead_found_by: "warning_ignored",
};

const FINDING_2 = {
  finding_id: 2,
  lead_id: null,
  question_id: null,
  source: SOURCE,
  symptom: "duplicate_call",
  symptom_text: "the agent called the same tool twice with the same arguments",
  explanation: "The agent repeated a call the first answer already covered.",
  found_by: "rule",
  model: null,
  decided_at: "2026-09-10T12:09:00+00:00",
  blames: [
    {
      root_cause: "tool_result_shape",
      root_cause_text: "a tool's own return did not say the call already ran",
      surface_kind: "tool",
      surface_name: "poll_workflow",
      why: "the second call at 6.2.0 repeats the first at 7.2.0",
    },
  ],
  lead_found_by: "duplicate_call",
};

const FINDINGS_LIST = [FINDING_1, FINDING_2];

const QUESTION_1 = {
  question_id: 7,
  conversation_id: "44444444-4444-4444-8444-444444444444",
  kind: "inspect",
  source: SOURCE,
  watch: "every-run",
  lead_id: null,
  tier: "helper",
  model: "z-ai/glm-5.3-flash",
  asked_at: "2026-09-10T12:16:00+00:00",
  answer: "Warning ignored at 5.8.0 to 5.9.2, two blames.",
};

const QUESTION_2 = {
  question_id: 8,
  conversation_id: "55555555-5555-5555-8555-555555555555",
  kind: "attribute",
  source: SOURCE,
  watch: "every-run",
  lead_id: 2,
  tier: "helper",
  model: "z-ai/glm-5.3-flash",
  asked_at: "2026-09-10T12:07:00+00:00",
  answer: "Lead dismissed, the retry changed the arguments.",
};

const JOB_1 = {
  id: 1,
  kind: "scan_run",
  key: `every-run:${SOURCE}`,
  state: "done",
  attempts: 1,
  result: "6 rules ran, 2 leads left",
  last_error: null,
  created_at: "2026-09-10T12:06:40+00:00",
  finished_at: "2026-09-10T12:06:41+00:00",
};

const JOB_2 = {
  id: 2,
  kind: "attribute_lead",
  key: "2",
  state: "done",
  attempts: 2,
  result: "attempt 2 dismissed the lead",
  last_error: null,
  created_at: "2026-09-10T12:07:02+00:00",
  finished_at: "2026-09-10T12:07:40+00:00",
};

const JOB_3 = {
  id: 3,
  kind: "inspect_run",
  key: `every-run:${SOURCE}`,
  state: "queued",
  attempts: 0,
  result: null,
  last_error: null,
  created_at: "2026-09-10T12:16:10+00:00",
  finished_at: null,
};

const OVERVIEW = {
  source: SOURCE,
  leads: [LEAD_SETTLED, LEAD_DISMISSED],
  findings: FINDINGS_LIST,
  questions: [QUESTION_1, QUESTION_2],
  jobs: [JOB_1, JOB_2, JOB_3],
};

const STEPS = [
  { step: 0, messages: 2, summary: "instructions loaded" },
  { step: 1, messages: 4, summary: "fetch_task answered" },
  { step: 2, messages: 6, summary: "read_file answered twice" },
  { step: 3, messages: 8, summary: "fetch_artifact errored, retried, answered" },
  { step: 4, messages: 5, summary: "list_registered answered" },
  { step: 5, messages: 12, summary: "fetched the workflow spec and ran it" },
  { step: 6, messages: 4, summary: "duplicate call" },
  { step: 7, messages: 4, summary: "duplicate call" },
  { step: 8, messages: 5, summary: "read_file answered" },
  { step: 9, messages: 2, summary: "final answer" },
];

const OUTLINE = {
  source: SOURCE,
  agent: "harness-author",
  started_at: "2026-09-10T12:02:00+00:00",
  ended_at: "2026-09-10T12:06:12+00:00",
  ended: "run_completed",
  model: "z-ai/glm-5.3",
  tool_calls: 11,
  steps: STEPS,
};

const STEP_5 = {
  step: 5,
  first: 0,
  cited: [],
  messages: [
    {
      run_id: RUN_ID,
      idx: 20,
      kind: "request",
      payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "Fetch the workflow spec." }] },
      text: "Fetch the workflow spec.",
    },
    {
      run_id: RUN_ID,
      idx: 21,
      kind: "response",
      payload: { kind: "response", parts: [{ part_kind: "text", content: "Done, the workflow has no steps." }] },
      text: "Done, the workflow has no steps.",
    },
  ],
};

function mountApi() {
  server.use(
    http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () => HttpResponse.json(OVERVIEW)),
    http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, () => HttpResponse.json(OUTLINE)),
    http.get(`${BASE}/api/inspection/runs/${RUN_ID}/steps/5`, () => HttpResponse.json(STEP_5)),
  );
}

describe("RunOverviewPage", () => {
  beforeEach(() => {
    vi.mocked(useParams).mockReturnValue({ run: RUN_ID });
  });

  it("shows the agent as the title with the short id beside it, and the meta line", async () => {
    mountApi();
    render(<RunOverviewPage />);

    expect(await screen.findByRole("heading", { name: "harness-author" })).toBeInTheDocument();
    expect(screen.getByText("d01d1796")).toBeInTheDocument();
    const meta = [
      `started ${longDate(OUTLINE.started_at)}`,
      duration(OUTLINE.started_at, OUTLINE.ended_at),
      "10 steps",
      "11 tool calls",
      "on z-ai/glm-5.3",
      "run completed",
    ].join(", ");
    // The model sits in its own mono span, so the sentence is split across elements: match on the
    // paragraph's own full text rather than a single text node.
    expect(screen.getByText((_, element) => element?.tagName === "P" && element.textContent === meta)).toBeInTheDocument();
    expect(screen.getByText("Click a step on the timeline to read it.")).toBeInTheDocument();
  });

  it("breadcrumbs to Inspection, names Runs, and carries the run id in mono", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: "Inspection" })).toHaveAttribute("href", "/inspection");
    expect(within(nav).getByText("Runs")).toBeInTheDocument();
    expect(within(nav).getByText(RUN_ID)).toBeInTheDocument();
  });

  it("draws the timeline's legend", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    expect(screen.getByText("step with a finding")).toBeInTheDocument();
    expect(screen.getByText("step with a lead the inspector dismissed")).toBeInTheDocument();
    expect(screen.getByText("no recorded finding, width is the step's message count")).toBeInTheDocument();
  });

  it("lists the leads with their settlement, the settled one linking to its finding", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    expect(screen.getByText("2, both settled")).toBeInTheDocument();

    expect(screen.getByRole("link", { name: "warning ignored" })).toHaveAttribute("href", "/inspection/1");
    expect(screen.getByText(/Yes, dismissed by the inspector:/)).toBeInTheDocument();
    expect(screen.getByText(LEAD_DISMISSED.refusal)).toHaveClass("text-danger-ink");
  });

  it("reads a dismissal with no refusal recorded without a trailing colon", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () =>
        HttpResponse.json({ ...OVERVIEW, leads: [{ ...LEAD_DISMISSED, refusal: null }] }),
      ),
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, () => HttpResponse.json(OUTLINE)),
    );
    render(<RunOverviewPage />);

    expect(await screen.findByText("Yes, dismissed by the inspector, no reason recorded.")).toBeInTheDocument();
  });

  it("lists the findings with who found them, chips and an Open link", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    const table = screen.getByRole("table", { name: "Findings" });
    expect(within(table).getByText(/inspector on/)).toBeInTheDocument();
    expect(within(table).getByText("duplicate_call")).toBeInTheDocument();
    const openLinks = within(table).getAllByRole("link", { name: "Open" });
    expect(openLinks[0]).toHaveAttribute("href", "/inspection/1");
    expect(openLinks[1]).toHaveAttribute("href", "/inspection/2");
    expect(within(table).getByText("model tier")).toBeInTheDocument();
    expect(within(table).getByText("fetch_workflow_spec")).toBeInTheDocument();
  });

  it("reads a finding's found-by as none when the model or the rule name is missing", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () =>
        HttpResponse.json({
          ...OVERVIEW,
          findings: [
            { ...FINDING_1, model: null },
            { ...FINDING_2, lead_found_by: null },
          ],
        }),
      ),
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, () => HttpResponse.json(OUTLINE)),
    );
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    const table = screen.getByRole("table", { name: "Findings" });
    // "inspector on" and "rule" each split across a plain text node and a mono/label span, so
    // the row's own cell is matched on its full text rather than a single text node.
    expect(
      within(table).getByText((_, element) => element?.tagName === "TD" && element.textContent === "inspector on none"),
    ).toBeInTheDocument();
    expect(
      within(table).getByText((_, element) => element?.tagName === "TD" && element.textContent === "rule none"),
    ).toBeInTheDocument();
  });

  it("lists the questions with their kind, tier, model, answer and a reading log link", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    const table = screen.getByRole("table", { name: "Questions asked" });
    expect(within(table).getByText("inspect")).toBeInTheDocument();
    expect(within(table).getByText("attribute")).toBeInTheDocument();
    expect(within(table).getAllByText("helper")).toHaveLength(2);
    expect(within(table).getByText("Warning ignored at 5.8.0 to 5.9.2, two blames.")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Reading log" })).toHaveAttribute("href", "/inspection/1#trace");
    expect(within(table).getByText("none")).toBeInTheDocument();
  });

  it("lists the jobs with the kind in mono, the lead's symptom beside attribute_lead, state, attempts and result", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    expect(screen.getByText("3, 1 still running")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Jobs the pipeline issued" });
    expect(within(table).getByText("scan_run")).toBeInTheDocument();
    expect(within(table).getByText("attribute_lead")).toBeInTheDocument();
    expect(within(table).getByText("retried after error")).toBeInTheDocument();
    expect(within(table).getByText("6 rules ran, 2 leads left")).toBeInTheDocument();
    expect(within(table).getByText("attempt 2 dismissed the lead")).toBeInTheDocument();
  });

  it("opens the step drawer when a bar in the timeline is clicked", async () => {
    mountApi();
    render(<RunOverviewPage />);

    await screen.findByRole("heading", { name: "harness-author" });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /^step \d+, \d+ messages$/ })).toHaveLength(10);
    });

    await userEvent.click(screen.getByRole("button", { name: "step 5, 12 messages" }));

    const dialog = await screen.findByRole("dialog", { name: "Step 5 of the run" });
    expect(within(dialog).getByText("Fetch the workflow spec.")).toBeInTheDocument();
  });

  it("still shows the id and renders the tables when the outline cannot be read", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () => HttpResponse.json(OVERVIEW)),
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, () => HttpResponse.json({ message: "not found" }, { status: 404 })),
    );
    render(<RunOverviewPage />);

    expect(await screen.findByText("d01d1796")).toBeInTheDocument();
    expect(await screen.findByText("The run itself can no longer be read.")).toBeInTheDocument();
    expect(screen.getByText("2, both settled")).toBeInTheDocument();
    expect(screen.getByText("3, 1 still running")).toBeInTheDocument();
  });

  it("shows an error sentence when the run itself cannot be opened", async () => {
    server.use(http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () => HttpResponse.json({ message: "not found" }, { status: 404 })));
    render(<RunOverviewPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(`Could not open run ${RUN_ID}.`);
  });

  it("waits for the outline to settle before calling the run gone, and renders the meta line once it lands", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () => HttpResponse.json(OVERVIEW)),
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(OUTLINE);
      }),
    );
    render(<RunOverviewPage />);

    expect(screen.queryByText("The run itself can no longer be read.")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "harness-author" })).not.toBeInTheDocument();
    expect(screen.getByText(`Opening run ${RUN_ID}.`)).toBeInTheDocument();

    await screen.findByRole("heading", { name: "harness-author" });
    expect(screen.queryByText("The run itself can no longer be read.")).not.toBeInTheDocument();

    const meta = [
      `started ${longDate(OUTLINE.started_at)}`,
      duration(OUTLINE.started_at, OUTLINE.ended_at),
      "10 steps",
      "11 tool calls",
      "on z-ai/glm-5.3",
      "run completed",
    ].join(", ");
    expect(screen.getByText((_, element) => element?.tagName === "P" && element.textContent === meta)).toBeInTheDocument();
  });

  it("still shows the header with 0 steps when the outline names no steps", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}`, () => HttpResponse.json(OVERVIEW)),
      http.get(`${BASE}/api/inspection/runs/${RUN_ID}/outline`, () => HttpResponse.json({ ...OUTLINE, steps: [] })),
    );
    render(<RunOverviewPage />);

    expect(await screen.findByRole("heading", { name: "harness-author" })).toBeInTheDocument();
    expect(screen.getByText(/0 steps/)).toBeInTheDocument();
  });
});
