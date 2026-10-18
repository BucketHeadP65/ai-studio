import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "@/api/inspection";
import { longDate } from "@/api/inspection-words";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { Trace } from "./trace";

const BASE = "http://studio.test";

const FINDING: Finding = {
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
  ],
  lead_found_by: "inspector",
};

// The finding's own evidence spans -- `Trace` no longer derives these itself, `FindingDetailView`
// passes them down (the same set `citesOf` falls back to when a why names none of them).
const SPANS = ["5.8.0", "5.9.0"];

function toolCall(name: string, id: string, args: Record<string, unknown> = {}) {
  return { part_kind: "tool-call", tool_name: name, tool_call_id: id, args };
}

function toldMessage(text: string) {
  return {
    run_id: "inspector-a1b2",
    idx: 0,
    kind: "request",
    text,
    payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] },
  };
}

const QUESTION = {
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
};

const FOLLOW_UP_TEXT = "Why did you blame the tool?";
const FOLLOW_UP_ANSWER = "Because the tool's own return fused the warning with the confirmation.";

// A conversation with 11 tool calls (outline, 8 reads, 2 finds), a thinking part, a first
// answer the taxonomy refuses (the older flat call shape), a second, accepted answer (the
// `{symptom, blames}` shape), and one follow-up already asked and answered -- the same shape
// the trace bar, its expanded rows and the meta line read from.
const MESSAGES = [
  toldMessage("Symptom: warning_ignored."),
  {
    run_id: "inspector-a1b2",
    idx: 1,
    kind: "response",
    text: "",
    payload: {
      kind: "response",
      parts: [
        toolCall("outline", "c-outline"),
        ...Array.from({ length: 8 }, (_, index) => toolCall("read", `c-read-${index}`, { step: index })),
        ...Array.from({ length: 2 }, (_, index) => toolCall("find", `c-find-${index}`, { q: `x${index}` })),
        { part_kind: "thinking", content: "Weighing the two candidates the evidence allows." },
        toolCall("final_result_Attribution", "c-final-1", {
          root_cause: "tool_result_shape",
          surface_kind: "tool",
          surface_name: "fetch_workflow_spec",
          why: "the return at 5.8.0 shows the tool answered with an empty steps list",
        }),
      ],
    },
  },
  {
    run_id: "inspector-a1b2",
    idx: 2,
    kind: "request",
    text: "",
    payload: { kind: "request", parts: [{ part_kind: "retry-prompt", tool_call_id: "c-final-1", content: "field required" }] },
  },
  {
    run_id: "inspector-a1b2",
    idx: 3,
    kind: "response",
    text: "",
    payload: {
      kind: "response",
      parts: [
        toolCall("final_result_Attribution", "c-final-2", {
          symptom: "warning_ignored",
          blames: [
            {
              root_cause: "agent_reasoning",
              surface_kind: "model_tier",
              surface_name: "",
              why: "the return at 5.8.0 said 're-check before trusting it', and the thinking at 5.9.0 never ran that check",
            },
          ],
          explanation: "The agent read the warning and ran no check before firing the workflow.",
        }),
      ],
    },
  },
  {
    run_id: "inspector-a1b2",
    idx: 4,
    kind: "request",
    text: FOLLOW_UP_TEXT,
    payload: { kind: "request", parts: [
      { part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "c-final-2", content: "Final result processed." },
      { part_kind: "user-prompt", content: FOLLOW_UP_TEXT },
    ] },
  },
  {
    run_id: "inspector-a1b2",
    idx: 5,
    kind: "response",
    text: FOLLOW_UP_ANSWER,
    payload: { kind: "response", parts: [{ part_kind: "text", content: FOLLOW_UP_ANSWER }] },
  },
];

const META_TEXT = `on claude-test, asked ${longDate(QUESTION.asked_at)}, 11 reads, first answer refused, 1 follow-up`;

function mountApi() {
  server.use(http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json({ question: QUESTION, messages: MESSAGES })));
}

function mountQuestion(messages: unknown[]) {
  server.use(http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json({ question: QUESTION, messages })));
}

async function expandTrace() {
  await userEvent.click(await screen.findByRole("button", { name: /Expand the trace/ }));
}

// A sentence's own words split it across a mono span (the model, a rule's key) and plain text
// siblings -- RTL's default text matcher reads only an element's own direct text-node
// children, so a sentence like this needs its full, rendered `textContent` matched instead.
function fullText(expected: string): (content: string, element: Element | null) => boolean {
  return (_content, element) => (element?.textContent ?? "").replace(/\s+/g, " ").trim() === expected;
}

describe("Trace", () => {
  beforeEach(() => {
    mountApi();
  });

  it("reads the meta line: the model, when asked, how many reads, the refusal and the follow-up", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);

    expect(await screen.findByText(fullText(META_TEXT))).toBeInTheDocument();
  });

  it("bars one row per question, chipped in call order, and a row per follow-up", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await screen.findByText(fullText(META_TEXT));

    const questionRow = screen.getByText("question").parentElement as HTMLElement;
    const questionChips = within(questionRow)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(questionChips.slice(0, 2)).toEqual(["what it was told", "outline()"]);
    expect(questionChips.slice(2, 10)).toEqual(Array.from({ length: 8 }, (_, index) => `read(step: ${index})`));
    expect(questionChips.slice(10, 12)).toEqual(['find(q: "x0")', 'find(q: "x1")']);
    expect(questionChips.slice(12)).toEqual(["thinking", "refused", "answered"]);

    const followUpRow = screen.getByText("follow-up").parentElement as HTMLElement;
    const followUpChips = within(followUpRow)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(followUpChips).toEqual(["you asked", "answered"]);
  });

  it("keeps the rows out of the document until expanded, shows every row kind, and folds again", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await screen.findByText(fullText(META_TEXT));

    expect(screen.queryByText("What it was told")).not.toBeInTheDocument();

    await expandTrace();

    expect(await screen.findByText("What it was told")).toBeInTheDocument();
    expect(screen.getAllByText("outline()").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Thinking").length).toBeGreaterThan(0);
    expect(screen.getByText("The answer was refused: field required")).toBeInTheDocument();
    expect(screen.getAllByText("Answered").length).toBeGreaterThan(0);
    expect(screen.getAllByText("diagnosis, pending verification")).toHaveLength(1);
    expect(screen.getByText("the answer it gave")).toBeInTheDocument();
    expect(screen.getAllByText("You asked").length).toBeGreaterThan(0);
    expect(screen.getAllByText((_, element) => element?.textContent === "claude-test answered").length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole("button", { name: /Fold the trace/ }));

    expect(screen.queryByText("What it was told")).not.toBeInTheDocument();
  });

  it("expands the trace and lights the refused row when its chip is clicked", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await screen.findByText(fullText(META_TEXT));

    await userEvent.click(screen.getByRole("button", { name: "refused" }));

    const refusedText = await screen.findByText("The answer was refused: field required");
    const row = refusedText.closest('[id^="trace-"]');
    expect(row).toHaveClass("bg-accent-soft");
  });

  it("reads the taxonomy's older flat call shape as one blame, with only its own content", async () => {
    mountQuestion([
      toldMessage("Symptom: warning_ignored."),
      {
        run_id: "inspector-a1b2",
        idx: 1,
        kind: "response",
        text: "",
        payload: {
          kind: "response",
          parts: [
            toolCall("final_result_Attribution", "c-flat", {
              root_cause: "tool_result_shape",
              surface_kind: "model_tier",
              surface_name: "",
              why: "the return at 5.8.0 shows the tool answered with an empty steps list",
            }),
          ],
        },
      },
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await expandTrace();

    expect(await screen.findByText("Tool result shape")).toBeInTheDocument();
    expect(screen.getByText(fullText("the model tier, nothing named to edit"))).toBeInTheDocument();
    expect(screen.getByText("the return at 5.8.0 shows the tool answered with an empty steps list")).toBeInTheDocument();
  });

  it("reads a scan's answer as one block per symptom it saw", async () => {
    mountQuestion([
      toldMessage("Scan this run."),
      {
        run_id: "inspector-a1b2",
        idx: 1,
        kind: "response",
        text: "",
        payload: {
          kind: "response",
          parts: [
            toolCall("final_result_Seen", "c-scan", {
              items: [
                {
                  symptom: "warning_ignored",
                  spans: ["5.8.0", "5.9.0", "5.9.1", "5.9.2", "5.11.0"],
                  blames: [
                    {
                      root_cause: "agent_reasoning",
                      surface_kind: "model_tier",
                      why: "The spec fetch at 5.8.0 returned fit STALE, but the next thinking (5.9.0) quotes only half.",
                    },
                    {
                      root_cause: "tool_result_shape",
                      surface_kind: "tool",
                      why: "The fit field is one prose blob.",
                    },
                  ],
                  explanation: "The agent read a warning and went on.",
                },
              ],
            }),
          ],
        },
      },
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await expandTrace();

    expect(await screen.findByText("warning ignored")).toBeInTheDocument();
    // The "spans" row and blame 2's "cites" row read the same text: its why names no
    // coordinate, so its cites fall back to the item's own five spans, not the finding's.
    expect(screen.getAllByText("5.8.0 5.9.0 5.9.1 5.9.2 5.11.0")).toHaveLength(2);
    expect(screen.getByText("blame 1")).toBeInTheDocument();
    expect(screen.getByText("blame 2")).toBeInTheDocument();
    expect(screen.getByText("Agent reasoning")).toBeInTheDocument();
    expect(screen.getByText(fullText("the model tier, nothing named to edit"))).toBeInTheDocument();
    expect(screen.getByText("The agent read a warning and went on.")).toBeInTheDocument();
  });

  it("reads a refusal to decide as one reason", async () => {
    const reason = "The window shows nothing a rule already flagged and nothing new to name.";
    mountQuestion([
      toldMessage("Symptom: warning_ignored."),
      {
        run_id: "inspector-a1b2",
        idx: 1,
        kind: "response",
        text: "",
        payload: { kind: "response", parts: [toolCall("final_result_NothingSeen", "c-reason", { reason })] },
      },
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await expandTrace();

    expect(await screen.findByText("reason")).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it("reads a text response before any follow-up as said, not answered", async () => {
    mountQuestion([
      toldMessage("Symptom: warning_ignored."),
      {
        run_id: "inspector-a1b2",
        idx: 1,
        kind: "response",
        text: "Let me look at the transcript first.",
        payload: { kind: "response", parts: [{ part_kind: "text", content: "Let me look at the transcript first." }] },
      },
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);

    const questionRow = (await screen.findByText("question")).parentElement as HTMLElement;
    expect(within(questionRow).getByRole("button", { name: "said" })).toBeInTheDocument();

    await userEvent.click(within(questionRow).getByRole("button", { name: "said" }));

    expect(await screen.findByText("Said")).toBeInTheDocument();
  });

  it("names the composer's textbox, gates Ask on empty text, and calls onDraft while typing", async () => {
    const onDraft = vi.fn();
    render(<Trace finding={FINDING} draft="" onDraft={onDraft} focusRequest={0} spans={SPANS} />);

    const textbox = await screen.findByRole("textbox", { name: "Ask the inspector about this finding" });
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();

    fireEvent.change(textbox, { target: { value: "why?" } });

    expect(onDraft).toHaveBeenCalledWith("why?");
  });

  it("renders the composer disabled, reading what the inspector saw, while the question is still loading", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/questions/7`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json({ question: QUESTION, messages: MESSAGES });
      }),
    );

    render(<Trace finding={FINDING} draft="why?" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);

    const textbox = screen.getByRole("textbox", { name: "Ask the inspector about this finding" });
    expect(textbox).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(screen.getAllByText("Reading what the inspector saw.")).toHaveLength(2);

    await waitFor(() => {
      expect(textbox).not.toBeDisabled();
    });
  });

  it("focuses the composer once it stops being disabled, for a focus request made while it was", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/questions/7`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        return HttpResponse.json({ question: QUESTION, messages: MESSAGES });
      }),
    );

    const { rerender } = render(<Trace finding={FINDING} draft='On blame 1, Agent reasoning: "why" ' onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    rerender(<Trace finding={FINDING} draft='On blame 1, Agent reasoning: "why" ' onDraft={vi.fn()} focusRequest={1} spans={SPANS} />);

    const textbox = screen.getByRole("textbox", { name: "Ask the inspector about this finding" });
    expect(textbox).toBeDisabled();
    expect(textbox).not.toHaveFocus();

    await waitFor(() => {
      expect(textbox).toHaveFocus();
    });
  });

  it("posts the draft to ask, and the refetched answer appears once", async () => {
    const recorded: (typeof MESSAGES)[number][] = [];
    let posted: unknown = null;
    server.use(
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json({ question: QUESTION, messages: [...MESSAGES, ...recorded] })),
      http.post(`${BASE}/api/inspection/questions/7/ask`, async ({ request }) => {
        posted = await request.json();
        const answer = "Because nothing in the instructions covered a warning.";
        recorded.push(
          {
            run_id: "inspector-a1b2",
            idx: 6,
            kind: "request",
            text: "why?",
            payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "why?" }] },
          },
          {
            run_id: "inspector-a1b2",
            idx: 7,
            kind: "response",
            text: answer,
            payload: { kind: "response", parts: [{ part_kind: "text", content: answer }] },
          },
        );
        return HttpResponse.json({ answer, conversation_id: QUESTION.conversation_id, model: "claude-test" });
      }),
    );

    render(<Trace finding={FINDING} draft="why?" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);
    await screen.findByRole("textbox", { name: "Ask the inspector about this finding" });
    await expandTrace();

    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText("Because nothing in the instructions covered a warning.")).toBeInTheDocument();
    await waitFor(() => {
      expect(posted).toEqual({ text: "why?" });
      expect(screen.getAllByText("Because nothing in the instructions covered a warning.")).toHaveLength(1);
    });
  });

  it("says a rule decided this one, with no textbox, when found_by is rule", () => {
    render(
      <Trace
        finding={{ ...FINDING, found_by: "rule", lead_found_by: "duplicate_call" }}
        draft=""
        onDraft={vi.fn()}
        focusRequest={0}
        spans={SPANS}
      />,
    );

    expect(
      screen.getByText(fullText("A rule decided this one. There is no conversation to read, the blame comes from the rule duplicate_call.")),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says the finding was decided before conversations were recorded, with no textbox, when there is no question", () => {
    render(<Trace finding={{ ...FINDING, question_id: null }} draft="" onDraft={vi.fn()} focusRequest={0} spans={SPANS} />);

    expect(screen.getByText(new RegExp(`^This finding was decided on ${longDate(FINDING.decided_at)}`))).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
