import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "@/api/inspection";
import { longDate } from "@/api/inspection-words";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

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

function toolCall(name: string, id: string, args: Record<string, unknown> = {}) {
  return { part_kind: "tool-call", tool_name: name, tool_call_id: id, args };
}

function toolReturn(name: string, id: string, content: unknown, outcome = "success") {
  return { part_kind: "tool-return", tool_name: name, tool_call_id: id, content, outcome };
}

// A thinking part the way a model that keeps its reasoning hidden returns it: no text, only an id.
function hiddenThinking(id: string) {
  return { part_kind: "thinking", content: "", id };
}

function row(idx: number, kind: "request" | "response", parts: unknown[], text = "") {
  return { run_id: "inspector-a1b2", idx, kind, text, payload: { kind, parts } };
}

function toldMessage(text: string) {
  return row(0, "request", [{ part_kind: "user-prompt", content: text }], text);
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

const TOLD_TEXT = "Symptom: warning_ignored.";
const THOUGHT = "Weighing the two candidates the evidence allows.";
const REFUSAL = "wrong_tool_chosen must cite a tool-call part, not retry-prompt at 2.6.0";
const FOLLOW_UP_TEXT = "Why did you blame the tool?";
const FOLLOW_UP_ANSWER = "Because the tool's own return fused the warning with the confirmation.";

// A conversation with 11 tool calls (outline, 8 reads, 2 finds), a hidden thinking part and one
// with text, a first answer the taxonomy refuses, a second, accepted answer, and one follow-up
// already asked and answered.
const MESSAGES = [
  toldMessage(TOLD_TEXT),
  row(1, "response", [
    hiddenThinking("rs-1"),
    toolCall("outline", "c-outline"),
    ...Array.from({ length: 8 }, (_, index) => toolCall("read", `c-read-${index}`, { step: index })),
    ...Array.from({ length: 2 }, (_, index) => toolCall("find", `c-find-${index}`, { text: `x${index}` })),
    { part_kind: "thinking", content: THOUGHT },
    toolCall("final_result_Attribution", "c-final-1", { symptom: "wrong_tool_chosen", blames: [] }),
  ]),
  row(2, "request", [
    toolReturn("outline", "c-outline", "step 0: asked the agent to review the merged object"),
    toolReturn("final_result_Attribution", "c-final-1", REFUSAL, "retried"),
  ]),
  row(3, "response", [
    hiddenThinking("rs-2"),
    toolCall("final_result_Attribution", "c-final-2", {
      symptom: "warning_ignored",
      blames: [{ root_cause: "agent_reasoning", surface_kind: "model_tier", surface_name: "", why: "the thinking at 5.9.0 never ran that check" }],
      explanation: "The agent read the warning and ran no check before firing the workflow.",
    }),
  ]),
  row(
    4,
    "request",
    [toolReturn("final_result_Attribution", "c-final-2", "Final result processed."), { part_kind: "user-prompt", content: FOLLOW_UP_TEXT }],
    FOLLOW_UP_TEXT,
  ),
  row(5, "response", [{ part_kind: "text", content: FOLLOW_UP_ANSWER }], FOLLOW_UP_ANSWER),
];

const META_TEXT = `on claude-test, asked ${longDate(QUESTION.asked_at)}, 11 reads, first answer refused, 1 follow-up`;

function mountQuestion(messages: unknown[]) {
  server.use(http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json({ question: QUESTION, messages })));
}

// A sentence's own words split it across a mono span (the model, a rule's key) and plain text
// siblings -- RTL's default text matcher reads only an element's own direct text-node
// children, so a sentence like this needs its full, rendered `textContent` matched instead.
function fullText(expected: string): (content: string, element: Element | null) => boolean {
  return (_content, element) => (element?.textContent ?? "").replace(/\s+/g, " ").trim() === expected;
}

// The chat names each assistant turn by who spoke, in a paragraph of its own above the bubble.
function speakerLabels(): HTMLElement[] {
  return screen.getAllByText("claude-test", { selector: "p" });
}

describe("Trace", () => {
  beforeEach(() => {
    mountQuestion(MESSAGES);
  });

  it("reads the meta line: the model, when asked, how many reads, the refusal and the follow-up", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(await screen.findByText(fullText(META_TEXT))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run overview" })).toHaveAttribute("href", "/inspection/runs/run-1");
  });

  it("draws what it was told and the follow-up as user turns, and the inspector's replies as turns named by its model", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(await screen.findByText(TOLD_TEXT)).toBeInTheDocument();
    expect(screen.getAllByText("You")).toHaveLength(2);
    expect(screen.getByText(FOLLOW_UP_TEXT)).toBeInTheDocument();
    expect(speakerLabels()).toHaveLength(2);
    expect(screen.getByText(FOLLOW_UP_ANSWER)).toBeInTheDocument();
  });

  it("brings the section into view once the conversation loads, only when the page was opened at #trace", async () => {
    const scroll = vi.mocked(Element.prototype.scrollIntoView);
    scroll.mockClear();
    const { unmount } = render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);
    await screen.findByText(TOLD_TEXT);
    expect(scroll).not.toHaveBeenCalled();
    unmount();

    window.location.hash = "#trace";
    try {
      render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);
      await screen.findByText(TOLD_TEXT);

      await waitFor(() => {
        expect(scroll.mock.contexts).toEqual([document.getElementById("trace")]);
      });
    } finally {
      window.location.hash = "";
    }
  });

  it("draws the inspector's calls as the chat's tool cards, the answer last with what its return said", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    const group = await screen.findByRole("button", { name: "Called 12 tools" });
    expect(screen.getByRole("button", { name: /final_result_Attribution.*Final result processed\./ })).toBeInTheDocument();

    await userEvent.click(group);

    expect(screen.getByRole("button", { name: /outline.*step 0: asked the agent to review the merged object/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Completed\s*read\s*pending$/ })).toHaveLength(8);
    expect(screen.getAllByRole("button", { name: /^Completed\s*find\s*pending$/ })).toHaveLength(2);
  });

  it("shows the refused answer as an error card whose body reads the refusal", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    await userEvent.click(await screen.findByRole("button", { name: "Called 12 tools" }));
    const refused = screen.getByRole("button", { name: /^Error\s*final_result_Attribution/ });

    await userEvent.click(refused);

    expect(await screen.findByText(REFUSAL)).toBeInTheDocument();
  });

  it("draws a refused text answer's refusal at its place, labelled as a refusal and never as a turn", async () => {
    const answered = "So the attributor run: steps 0-4.";
    const refusal = "Invalid JSON: expected value at line 1 column 1";
    mountQuestion([
      toldMessage(TOLD_TEXT),
      row(1, "response", [{ part_kind: "text", content: answered }], answered),
      row(2, "request", [
        {
          part_kind: "retry-feedback",
          content: [{ type: "json_invalid", loc: [], msg: refusal, input: answered }],
          retry: { cause: "validation_error", attempt: null, max_retries: null },
        },
      ]),
      row(3, "response", [toolCall("final_result_Seen", "c-final", { items: [] })]),
      row(4, "request", [toolReturn("final_result_Seen", "c-final", "Final result processed.")]),
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    const refusalText = await screen.findByText(refusal);
    const note = refusalText.parentElement as HTMLElement;
    expect(within(note).getByText("Refused")).toBeInTheDocument();
    expect(within(note).queryByText("claude-test")).not.toBeInTheDocument();
    expect(screen.getAllByText("You")).toHaveLength(1);
    expect(speakerLabels()).toHaveLength(2);
    const answer = screen.getByText(answered);
    const retry = screen.getByRole("button", { name: /final_result_Seen\s*Final result processed\./ });
    expect(answer.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(note.compareDocumentPosition(retry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("draws a thinking part with text as the chat draws it, its note holding no block for the hidden ones", async () => {
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(await screen.findByText(THOUGHT)).toBeInTheDocument();
    const label = screen.getByText("Thinking");
    // The note is its label and one block per reasoning part it draws: here the one with text.
    expect((label.parentElement as HTMLElement).children).toHaveLength(2);
  });

  it("draws nothing for a conversation whose thinking is all hidden", async () => {
    mountQuestion([
      toldMessage(TOLD_TEXT),
      row(1, "response", [hiddenThinking("rs-1"), hiddenThinking("rs-2"), toolCall("outline", "c-outline")]),
      row(2, "request", [toolReturn("outline", "c-outline", "step 0: asked the agent to review")]),
      row(3, "response", [hiddenThinking("rs-3")]),
    ]);
    render(<Trace finding={FINDING} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(await screen.findByRole("button", { name: /outline.*step 0: asked the agent to review/ })).toBeInTheDocument();
    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
    expect(speakerLabels()).toHaveLength(1);
  });

  it("names the composer's textbox, gates Ask on empty text, and calls onDraft while typing", async () => {
    const onDraft = vi.fn();
    render(<Trace finding={FINDING} draft="" onDraft={onDraft} focusRequest={0} />);

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

    render(<Trace finding={FINDING} draft="why?" onDraft={vi.fn()} focusRequest={0} />);

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

    const { rerender } = render(<Trace finding={FINDING} draft='On blame 1, Agent reasoning: "why" ' onDraft={vi.fn()} focusRequest={0} />);
    rerender(<Trace finding={FINDING} draft='On blame 1, Agent reasoning: "why" ' onDraft={vi.fn()} focusRequest={1} />);

    const textbox = screen.getByRole("textbox", { name: "Ask the inspector about this finding" });
    expect(textbox).toBeDisabled();
    expect(textbox).not.toHaveFocus();

    await waitFor(() => {
      expect(textbox).toHaveFocus();
    });
  });

  it("asks once on Return, and Shift+Return keeps the newline without asking", async () => {
    const posted: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/inspection/questions/7/ask`, async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({ answer: "Because.", conversation_id: QUESTION.conversation_id, model: "claude-test" });
      }),
    );
    render(<Trace finding={FINDING} draft="why?" onDraft={vi.fn()} focusRequest={0} />);
    const textbox = await screen.findByRole("textbox", { name: "Ask the inspector about this finding" });
    await waitFor(() => expect(textbox).not.toBeDisabled());

    fireEvent.keyDown(textbox, { key: "Enter", code: "Enter", shiftKey: true });
    fireEvent.keyUp(textbox, { key: "Enter", code: "Enter", shiftKey: true });
    expect(posted).toEqual([]);

    fireEvent.keyDown(textbox, { key: "Enter", code: "Enter" });
    fireEvent.keyUp(textbox, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(posted).toEqual([{ text: "why?" }]));
  });

  it("posts the draft to ask, and the question and its answer appear once each as chat turns", async () => {
    const recorded: unknown[] = [];
    let posted: unknown = null;
    const answer = "Because nothing in the instructions covered a warning.";
    server.use(
      http.get(`${BASE}/api/inspection/questions/7`, () => HttpResponse.json({ question: QUESTION, messages: [...MESSAGES, ...recorded] })),
      http.post(`${BASE}/api/inspection/questions/7/ask`, async ({ request }) => {
        posted = await request.json();
        recorded.push(
          row(6, "request", [{ part_kind: "user-prompt", content: "why?" }], "why?"),
          row(7, "response", [{ part_kind: "text", content: answer }], answer),
        );
        return HttpResponse.json({ answer, conversation_id: QUESTION.conversation_id, model: "claude-test" });
      }),
    );

    render(<Trace finding={FINDING} draft="why?" onDraft={vi.fn()} focusRequest={0} />);
    await screen.findByText(FOLLOW_UP_ANSWER);
    expect(speakerLabels()).toHaveLength(2);

    await userEvent.click(screen.getByRole("button", { name: "Ask" }));

    expect(await screen.findByText(answer)).toBeInTheDocument();
    await waitFor(() => {
      expect(posted).toEqual({ text: "why?" });
      expect(screen.getAllByText(answer)).toHaveLength(1);
      expect(screen.getAllByText("You")).toHaveLength(3);
      expect(speakerLabels()).toHaveLength(3);
    });
    const asked = screen.getAllByText("You")[2].parentElement as HTMLElement;
    expect(within(asked).getByText("why?")).toBeInTheDocument();
  });

  it("says a rule decided this one, with no textbox, when found_by is rule", () => {
    render(<Trace finding={{ ...FINDING, found_by: "rule", lead_found_by: "duplicate_call" }} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(
      screen.getByText(fullText("A rule decided this one. There is no conversation to read, the blame comes from the rule duplicate_call.")),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("says the finding was decided before conversations were recorded, with no textbox, when there is no question", () => {
    render(<Trace finding={{ ...FINDING, question_id: null }} draft="" onDraft={vi.fn()} focusRequest={0} />);

    expect(screen.getByText(new RegExp(`^This finding was decided on ${longDate(FINDING.decided_at)}`))).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
