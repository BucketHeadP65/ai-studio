import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { longDate } from "@/api/inspection-words";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

vi.mock("next/navigation", () => ({
  useParams: vi.fn(() => ({ id: "1" })),
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the page sees the mocked module.
import { useParams } from "next/navigation";

import FindingPage from "./page";

const BASE = "http://studio.test";

const DETAIL = {
  finding: {
    finding_id: 1,
    lead_id: 1,
    question_id: 7,
    source: "record:run-1",
    symptom: "warning_ignored",
    symptom_text: "the transcript shows a warning the agent never acted on",
    explanation: "The agent read a stale warning as a clean confirmation and ran the workflow anyway.",
    found_by: "inspector",
    model: "claude-test",
    decided_at: "2026-09-10T12:00:00+00:00",
    blames: [
      {
        root_cause: "agent_reasoning",
        root_cause_text: "the agent erred though it had what it needed",
        surface_kind: "model_tier",
        surface_name: "",
        why: "the return at 5.8.0 said 're-check before trusting it', and the thinking at 5.9.0 never ran that check",
      },
      {
        root_cause: "tool_result_shape",
        root_cause_text: "a tool's own return mixed the warning with a confirmation",
        surface_kind: "tool",
        surface_name: "fetch_workflow_spec",
        why: "the model's own thinking says 'fits here', but the warning right before it says otherwise",
      },
    ],
    lead_found_by: "inspector",
  },
  lead: {
    lead_id: 1,
    source: "record:run-1",
    watch: "every-run",
    symptom: "warning_ignored",
    spans: ["5.8.0", "5.9.0", "5.9.2"],
    found_by: "inspector",
    seen_at: "2026-09-10T12:00:00+00:00",
    attempted_at: null,
    refusal: null,
    resolution: "open",
  },
  evidence: [
    {
      span: "5.8.0",
      label: "step 5, the return of fetch_workflow_spec",
      kind: "tool-return",
      text: "output: workflow spec\nsummary: looked fine on the surface\nfit: STALE: re-check before trusting it\nfits_here: true",
    },
    {
      span: "5.9.0",
      label: "step 5, the model's thinking",
      kind: "thinking",
      text: "The spec fits here, the leaves are registered.",
    },
    {
      span: "5.9.2",
      label: "step 5, the model's call to research-digest",
      kind: "tool-call",
      text: "name: research-digest",
    },
  ],
};

function otherFinding(id: number) {
  return {
    ...DETAIL.finding,
    finding_id: id,
    lead_id: null,
    question_id: null,
    symptom: "missing_tool",
    symptom_text: "no tool exists for the job",
    found_by: "rule",
    model: null,
    blames: [],
    lead_found_by: "missing_tool_detector",
  };
}

const FINDINGS_LIST = [otherFinding(3), DETAIL.finding, otherFinding(9)];

const OUTLINE = {
  source: "record:run-1",
  agent: "harness-author-d01d1796",
  started_at: "2026-09-10T11:55:00+00:00",
  ended_at: "2026-09-10T12:01:00+00:00",
  ended: "success",
  model: "claude-test",
  tool_calls: 9,
  steps: [
    { step: 0, messages: 2, summary: "opened the run" },
    { step: 1, messages: 3, summary: "read the request" },
    { step: 2, messages: 2, summary: "checked the plan" },
    { step: 3, messages: 4, summary: "gathered the leaves" },
    { step: 4, messages: 2, summary: "digested the base" },
    { step: 5, messages: 12, summary: "fetched the workflow spec and ran it" },
    { step: 6, messages: 3, summary: "wrote the report" },
  ],
};

const STEP_5 = {
  step: 5,
  first: 0,
  cited: [2],
  messages: [
    {
      run_id: "run-1",
      idx: 10,
      kind: "request",
      payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "Fetch the workflow spec." }] },
      text: "Fetch the workflow spec.",
    },
    {
      run_id: "run-1",
      idx: 11,
      kind: "response",
      payload: {
        kind: "response",
        parts: [{ part_kind: "tool-call", tool_name: "fetch_workflow_spec", tool_call_id: "call-1", args: { id: "wf.report.v1" } }],
      },
      text: "",
    },
    {
      run_id: "run-1",
      idx: 12,
      kind: "request",
      payload: {
        kind: "request",
        parts: [{ part_kind: "tool-return", tool_name: "fetch_workflow_spec", tool_call_id: "call-1", content: '{"steps": []}' }],
      },
      text: "",
    },
    {
      run_id: "run-1",
      idx: 13,
      kind: "response",
      payload: { kind: "response", parts: [{ part_kind: "text", content: "Done, the workflow has no steps." }] },
      text: "Done, the workflow has no steps.",
    },
  ],
};

function toolCall(name: string, id: string, args: Record<string, unknown> = {}) {
  return { part_kind: "tool-call", tool_name: name, tool_call_id: id, args };
}

// A conversation whose tool calls read as "outline 1, read 8, find 2" once counted, its own
// final answer excluded from that count.
const QUESTION = {
  question: {
    question_id: 7,
    conversation_id: "44444444-4444-4444-8444-444444444444",
    kind: "attribute",
    source: "record:run-1",
    watch: "every-run",
    lead_id: 1,
    tier: "helper",
    model: "claude-test",
    asked_at: "2026-09-10T12:00:00+00:00",
    answer: "lead 1: finding 1 by claude-test",
  },
  messages: [
    {
      run_id: "inspector-a1b2",
      idx: 0,
      kind: "request",
      payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "Symptom: warning_ignored." }] },
      text: "Symptom: warning_ignored.",
    },
    {
      run_id: "inspector-a1b2",
      idx: 1,
      kind: "response",
      payload: {
        kind: "response",
        parts: [
          toolCall("outline", "c-outline"),
          ...Array.from({ length: 8 }, (_, index) => toolCall("read", `c-read-${index}`, { step: index })),
          ...Array.from({ length: 2 }, (_, index) => toolCall("find", `c-find-${index}`, { q: `x${index}` })),
          toolCall("final_result_Attribution", "c-final", {
            root_cause: "tool_result_shape",
            surface_kind: "tool",
            surface_name: "fetch_workflow_spec",
            why: "the return at 5.8.0 shows the tool answered with an empty steps list",
          }),
        ],
      },
      text: "",
    },
  ],
};

function mountApi() {
  server.use(
    http.get(`${BASE}/api/inspection/findings/1`, () => HttpResponse.json(DETAIL)),
    http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
    http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
    http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json(QUESTION)),
  );
}

// The judgment table's two blame rows only, telling them apart from its own header row by the
// header's cells being `columnheader`, not `cell`.
function blameRows(): HTMLElement[] {
  return screen.getAllByRole("row").filter((row) => within(row).queryAllByRole("cell").length > 0);
}

describe("FindingPage", () => {
  beforeEach(() => {
    vi.mocked(useParams).mockReturnValue({ id: "1" });
  });

  it("shows the header's symptom name and the taxonomy's sentence for it", async () => {
    mountApi();
    render(<FindingPage />);

    expect(await screen.findByRole("heading", { name: "warning ignored" })).toBeInTheDocument();
    expect(screen.getByText(DETAIL.finding.symptom_text)).toBeInTheDocument();
  });

  it("links to the run the finding is about", async () => {
    mountApi();
    render(<FindingPage />);

    expect(await screen.findByRole("link", { name: "Open run" })).toHaveAttribute("href", "/inspection/runs/run-1");
  });

  it("reads the meta line as found by the inspector on none, when the finding has no recorded model", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({ ...DETAIL, finding: { ...DETAIL.finding, model: null } }),
      ),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json(QUESTION)),
    );
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName === "P" &&
          element.textContent === `Found by the inspector on none, ${longDate(DETAIL.finding.decided_at)}, run run-1`,
      ),
    ).toBeInTheDocument();
  });

  it("reads one span and one blame as singular", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({
          ...DETAIL,
          finding: { ...DETAIL.finding, blames: [DETAIL.finding.blames[0]] },
          evidence: [DETAIL.evidence[0]],
        }),
      ),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json(QUESTION)),
    );
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    expect(screen.getByText("1 span, all in step 5")).toBeInTheDocument();
    expect(screen.getByText("1 blame, hover a row to light the spans it cites")).toBeInTheDocument();
  });

  it("places the finding in the list with prev and next", async () => {
    mountApi();
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });

    expect(await screen.findByText("2 of 3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "prev" })).toHaveAttribute("href", "/inspection/3");
    expect(screen.getByRole("link", { name: "next" })).toHaveAttribute("href", "/inspection/9");
  });

  it("judges in one table: root cause, fix surface, why with its coordinates, cites", async () => {
    mountApi();
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    const rows = blameRows();
    expect(rows).toHaveLength(2);

    expect(within(rows[0]).getByText("Agent reasoning")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Model tier")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Nothing named to edit")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Tool result shape")).toBeInTheDocument();
    expect(within(rows[1]).getByText("fetch_workflow_spec")).toBeInTheDocument();

    const cites1 = within(rows[0].children[4] as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent);
    const cites2 = within(rows[1].children[4] as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(cites1).toEqual(["5.8.0", "5.9.0"]);
    expect(cites2).toEqual(["5.8.0", "5.9.0", "5.9.2"]);
  });

  it("tells the story: one card per span with its key line, the first picked", async () => {
    mountApi();
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    const card = screen.getByRole("button", { name: "step 5, the return of fetch_workflow_spec" });
    expect(card.textContent).toContain("fit: STALE: re-check before trusting it");
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
  });

  it("picks a span from a why's coordinate and from the next button", async () => {
    mountApi();
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    const rows = blameRows();
    const whyCell = within(rows[0].children[3] as HTMLElement);
    await userEvent.click(whyCell.getByRole("button", { name: "5.9.0" }));

    expect(screen.getByText("step 5, the model's thinking")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next span" }));

    expect(screen.getByText("step 5, the model's call to research-digest")).toBeInTheDocument();
  });

  it("lights the cards a hovered blame cites", async () => {
    mountApi();
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    const rows = blameRows();
    fireEvent.mouseOver(rows[0]);

    const card580 = screen.getByRole("button", { name: "step 5, the return of fetch_workflow_spec" });
    const card590 = screen.getByRole("button", { name: "step 5, the model's thinking" });
    const card592 = screen.getByRole("button", { name: "step 5, the model's call to research-digest" });

    expect(card580).toHaveClass("border-accent");
    expect(card590).toHaveClass("border-accent");
    expect(card592).not.toHaveClass("border-accent");
  });

  it("draws the timeline from the outline and reads a step in the drawer", async () => {
    mountApi();
    server.use(http.get(`${BASE}/api/inspection/runs/run-1/steps/5`, () => HttpResponse.json(STEP_5)));
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /^step \d+, \d+ messages$/ })).toHaveLength(7);
    });

    await userEvent.click(screen.getByRole("button", { name: "Read step 5 in the run" }));

    const dialog = await screen.findByRole("dialog", { name: "Step 5 of the run" });
    expect(within(dialog).getByText("Fetch the workflow spec.")).toBeInTheDocument();
  });

  it("says the span is not in the run when the evidence says so", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({
          ...DETAIL,
          evidence: [{ span: "5.8.0", label: "step 5, a gone span", kind: "unreadable", text: "this span is not in the run" }],
        }),
      ),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json(QUESTION)),
    );
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });

    expect(screen.getAllByText("This span is not in the run.")).toHaveLength(2);
  });

  it("shows there is no evidence rather than crashing when a finding cites none", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () => HttpResponse.json({ ...DETAIL, evidence: [] })),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json(QUESTION)),
    );
    render(<FindingPage />);

    expect(await screen.findByRole("heading", { name: "warning ignored" })).toBeInTheDocument();
    expect(screen.getByText("There is no evidence to show.")).toBeInTheDocument();
  });

  it("appends the follow-up once, named by the model that actually answered it", async () => {
    mountApi();
    const FOLLOW_UP_ANSWER = "Because nothing in the instructions covered a warning.";
    const recorded: (typeof QUESTION.messages)[number][] = [];
    server.use(
      http.get(`${BASE}/api/inspection/questions/7`, () =>
        HttpResponse.json({ ...QUESTION, messages: [...QUESTION.messages, ...recorded] }),
      ),
      http.post(`${BASE}/api/inspection/questions/7/ask`, async ({ request }) => {
        const { text } = (await request.json()) as { text: string };
        recorded.push(
          {
            run_id: "inspector-a1b2",
            idx: recorded.length + 5,
            kind: "request",
            payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] },
            text,
          },
          {
            run_id: "inspector-a1b2",
            idx: recorded.length + 6,
            kind: "response",
            payload: { kind: "response", parts: [{ part_kind: "text", content: FOLLOW_UP_ANSWER }] },
            text: FOLLOW_UP_ANSWER,
          },
        );
        return HttpResponse.json({
          answer: FOLLOW_UP_ANSWER,
          conversation_id: "44444444-4444-4444-8444-444444444444",
          model: "test-model",
        });
      }),
    );
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    await userEvent.click(await screen.findByRole("button", { name: /Expand the trace/ }));
    const textbox = screen.getByRole("textbox", { name: "Ask the inspector about this finding" });
    await userEvent.type(textbox, "why not the instructions?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(screen.queryByText("Answered by test-model.")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("why not the instructions?")).toHaveLength(1);
      expect(
        screen.getAllByText((_, element) => element?.textContent === "claude-test answered").length,
      ).toBeGreaterThan(0);
      expect(screen.getAllByText(FOLLOW_UP_ANSWER)).toHaveLength(1);
    });
  });

  it("lands a challenge on a blame in the composer with the blame quoted, even clicked before the composer is ready", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () => HttpResponse.json(DETAIL)),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
      http.get(`${BASE}/api/inspection/questions/7`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json(QUESTION);
      }),
    );
    render(<FindingPage />);

    await screen.findByRole("heading", { name: "warning ignored" });
    // The composer mounts as soon as the finding has a question, disabled until this delayed
    // handler answers -- clicking Challenge here exercises the race, not just the happy path.
    await userEvent.click(screen.getAllByRole("button", { name: "Challenge" })[0]);

    const textbox = await screen.findByRole("textbox", { name: "Ask the inspector about this finding" });
    await waitFor(() => {
      expect((textbox as HTMLTextAreaElement).value).toMatch(/^On blame 1, Agent reasoning: "/);
      expect(textbox).toHaveFocus();
    });
  });

  it("opens with the trace expanded at #trace", async () => {
    mountApi();
    window.location.hash = "#trace";
    try {
      render(<FindingPage />);

      await screen.findByRole("heading", { name: "warning ignored" });
      expect(await screen.findByText("What it was told")).toBeInTheDocument();
    } finally {
      window.location.hash = "";
    }
  });

  it("says so when a rule decided the finding and there is no conversation to read", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({ ...DETAIL, finding: { ...DETAIL.finding, found_by: "rule" } }),
      ),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
    );
    render(<FindingPage />);

    expect(
      await screen.findByText(
        (_, element) =>
          element?.textContent ===
          "A rule decided this one. There is no conversation to read, the blame comes from the rule inspector.",
      ),
    ).toBeInTheDocument();
  });

  it("says so when the inspector decided before its conversation was recorded", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({ ...DETAIL, finding: { ...DETAIL.finding, question_id: null } }),
      ),
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: FINDINGS_LIST })),
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () => HttpResponse.json(OUTLINE)),
    );
    render(<FindingPage />);

    expect(
      await screen.findByText((content) => content.startsWith("This finding was decided on")),
    ).toBeInTheDocument();
  });

  it("shows an error sentence when the finding cannot be opened", async () => {
    server.use(http.get(`${BASE}/api/inspection/findings/1`, () => HttpResponse.json({ message: "not found" }, { status: 404 })));
    render(<FindingPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not open finding 1.");
  });

  it("shows the not-here sentence rather than opening forever when the id in the address is not a number", async () => {
    vi.mocked(useParams).mockReturnValue({ id: "abc" });
    render(<FindingPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not open finding abc.");
  });
});
