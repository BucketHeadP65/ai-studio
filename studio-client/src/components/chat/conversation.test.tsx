import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import type { UIMessage } from "ai";
import type { StickToBottomInstance, StickToBottomState } from "use-stick-to-bottom";
import { StickToBottom } from "use-stick-to-bottom";
import { describe, expect, it, vi } from "vitest";

import type { RunView, TranscriptMessage } from "@/api/sessions-types";
import type { ChatReply, PendingGate } from "@/api/studio-types";
import { ConversationScrollButton } from "@/components/ai-elements/conversation";
import { render } from "@/shared/test-support/render";

import { Conversation } from "./conversation";

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] };
}

// A ref that behaves as both the mutable object and the callback the library assigns through,
// with no real DOM element behind it -- enough for a fake StickToBottomInstance to type-check.
function fakeElementRef(): StickToBottomInstance["scrollRef"] {
  const ref = ((node: HTMLElement | null) => {
    ref.current = node;
  }) as StickToBottomInstance["scrollRef"];
  ref.current = null;
  return ref;
}

// Bypasses the library's own DOM-measured geometry entirely (StickToBottom takes this whole
// instance in place of the one its internal hook would compute), so `isAtBottom` is driven
// directly rather than through jsdom scroll/resize behavior the library was never asked to fake.
function fakeStickInstance(isAtBottom: boolean): StickToBottomInstance {
  const state: StickToBottomState = {
    scrollTop: 0,
    targetScrollTop: 0,
    calculatedTargetScrollTop: 0,
    scrollDifference: 0,
    resizeDifference: 0,
    velocity: 0,
    accumulated: 0,
    escapedFromLock: false,
    isAtBottom,
    isNearBottom: isAtBottom,
  };
  return {
    contentRef: fakeElementRef(),
    scrollRef: fakeElementRef(),
    scrollToBottom: vi.fn(),
    stopScroll: vi.fn(),
    isAtBottom,
    isNearBottom: isAtBottom,
    escapedFromLock: false,
    state,
  };
}

describe("ConversationScrollButton", () => {
  it("renders the aria-labelled control once the reader has scrolled away from the bottom", () => {
    render(
      <StickToBottom instance={fakeStickInstance(false)}>
        <ConversationScrollButton aria-label="Scroll to bottom" />
      </StickToBottom>,
    );
    expect(screen.getByRole("button", { name: "Scroll to bottom" })).toBeInTheDocument();
  });

  it("renders nothing while already at the bottom", () => {
    render(
      <StickToBottom instance={fakeStickInstance(true)}>
        <ConversationScrollButton aria-label="Scroll to bottom" />
      </StickToBottom>,
    );
    expect(screen.queryByRole("button", { name: "Scroll to bottom" })).not.toBeInTheDocument();
  });

  it("scrolls to the bottom when clicked", async () => {
    const instance = fakeStickInstance(false);
    render(
      <StickToBottom instance={instance}>
        <ConversationScrollButton aria-label="Scroll to bottom" />
      </StickToBottom>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Scroll to bottom" }));

    expect(instance.scrollToBottom).toHaveBeenCalled();
  });
});

function baseProps() {
  return {
    transcript: [] as TranscriptMessage[],
    streaming: false,
    reply: null as ChatReply | null,
    sessionId: "s1" as string | null,
    gateMode: "wait" as const,
    hasLiveRun: false,
    onChangeGates: vi.fn(),
    liveRun: null,
    liveGate: null,
    onDecide: vi.fn(),
    deciding: false,
    onInspect: vi.fn(),
  };
}

const REPLY: ChatReply = {
  message: "Drafted lit-check.",
  draft: { spec_yaml: "", title: "lit-check", summary: "", applicability: "" },
  issues: [],
  valid: true,
  intent_issues: [],
  graph: {
    name: "lit-check",
    version: 1,
    nodes: [
      {
        key: "a",
        id: "a",
        kind: "run",
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
    ],
    edges: [],
  },
};

const PENDING: PendingGate = {
  gate_id: "gate-1",
  workflow: "wf.lit-check.v5",
  reviewing: '{"title": "a draft"}',
  rejection_reruns: "compose",
};

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "s1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    state: "running",
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T00:00:00.000Z",
    ...overrides,
  };
}

function userPromptPayload(text: string) {
  return { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] };
}

function toolCallPayload(toolName: string, args: unknown) {
  return { kind: "response", parts: [{ part_kind: "tool-call", tool_name: toolName, args }] };
}

describe("Conversation", () => {
  it("renders user text plainly", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "build me a **literature check**")]}
      />,
    );
    expect(screen.getByText("build me a **literature check**")).toBeInTheDocument();
  });

  it("renders assistant text through Markdown", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Drafted **lit-check**.")]}
      />,
    );
    expect(screen.getByText("lit-check")).toBeInTheDocument(); // the <strong>, not raw asterisks
  });

  it("marks the transcript so a screen reader does not announce it as it streams", () => {
    render(<Conversation {...baseProps()} messages={[textMessage("1", "user", "hello")]} />);
    expect(screen.getByRole("log")).toHaveAttribute("aria-live", "off");
  });

  it("stays at the bottom with no scroll button while the reader has not scrolled up", () => {
    render(<Conversation {...baseProps()} messages={[textMessage("1", "user", "hello")]} />);
    expect(screen.queryByRole("button", { name: "Scroll to bottom" })).not.toBeInTheDocument();
  });

  it("keeps new messages from forcing a jump: the stick-to-bottom root renders instead of a scroll effect", () => {
    const { rerender } = render(
      <Conversation {...baseProps()} messages={[textMessage("1", "user", "hello")]} />,
    );
    // Cleared here, right before the action this test is actually about, so a call the
    // initial mount made (or a prior test left behind) never counts against the rerender.
    vi.mocked(Element.prototype.scrollIntoView).mockClear();
    rerender(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "hello"), textMessage("2", "assistant", "hi there")]}
      />,
    );
    // No scroll-into-view effect drives this component anymore; the vendored
    // StickToBottom root owns anchoring, and jsdom's default (untouched) geometry
    // reads as already at the bottom, so no scroll button appears.
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Scroll to bottom" })).not.toBeInTheDocument();
  });

  it("renders the gate turn the moment it opens, with no scroll-into-view effect involved", () => {
    const { rerender } = render(
      <Conversation {...baseProps()} messages={[textMessage("1", "user", "run it")]} />,
    );
    vi.mocked(Element.prototype.scrollIntoView).mockClear();
    rerender(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "run it")]}
        liveGate={{ pending: PENDING, startedAt: "2026-08-24T00:00:00.000Z", events: [] }}
      />,
    );
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("shows the draft card under the last assistant message when the reply carries a graph", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Drafted lit-check.")]}
        reply={REPLY}
      />,
    );
    expect(screen.getByText("lit-check")).toBeInTheDocument();
    expect(screen.getByText("Ready to run")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start this run" })).toBeInTheDocument();
  });

  it("shows no draft card when the reply carries no graph", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Just talking, no draft yet.")]}
        reply={{ ...REPLY, graph: null }}
      />,
    );
    expect(screen.queryByRole("button", { name: "Start this run" })).not.toBeInTheDocument();
  });

  it("passes the rail's gate mode and Change the gates through to the draft card", async () => {
    const onChangeGates = vi.fn();
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Drafted lit-check.")]}
        reply={REPLY}
        onChangeGates={onChangeGates}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Change the gates" }));
    expect(onChangeGates).toHaveBeenCalledTimes(1);
  });

  it("disables Start this run once this session already has a live run", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Drafted lit-check.")]}
        reply={REPLY}
        hasLiveRun
      />,
    );
    expect(screen.getByRole("button", { name: "Start this run" })).toBeDisabled();
  });

  it("renders assistant text verbatim even when it parses as an object with a message field", () => {
    const text = '{"message":"inner","other":"kept"}';
    render(<Conversation {...baseProps()} messages={[textMessage("1", "assistant", text)]} />);
    expect(
      screen.getByText(
        (_, element) => element?.tagName.toLowerCase() === "p" && element.textContent === text,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("inner", { exact: true })).not.toBeInTheDocument();
  });

  it("fires onInspect with the message when an assistant bubble is clicked", async () => {
    const onInspect = vi.fn();
    const message = textMessage("1", "assistant", "Drafted lit-check.");
    render(<Conversation {...baseProps()} messages={[message]} onInspect={onInspect} />);

    screen.getByRole("button", { name: "View turn detail" }).click();

    expect(onInspect).toHaveBeenCalledWith(message);
  });

  it("leaves out the agent bubble when a turn's whole answer was starting a run", () => {
    const transcript: TranscriptMessage[] = [
      { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: toolCallPayload("run_workflow", { workflow_id: "wf.lit-check.v5", arguments: {} }),
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "response",
        payload: { kind: "response", parts: [{ part_kind: "text", content: "" }] },
        text: "",
      },
    ];
    render(
      <Conversation
        {...baseProps()}
        transcript={transcript}
        messages={[textMessage("r1:0", "user", "go"), textMessage("r1:2", "assistant", "")]}
      />,
    );

    expect(
      screen.getByText(
        (_, element) => element?.textContent === "The agent started a run of wf.lit-check.v5.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View turn detail" })).toBeNull();
  });

  it("keeps the agent bubble when a run-starting turn also did work", () => {
    const transcript: TranscriptMessage[] = [
      { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: toolCallPayload("run_workflow", { workflow_id: "wf.lit-check.v5", arguments: {} }),
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "response",
        payload: { kind: "response", parts: [{ part_kind: "text", content: "" }] },
        text: "",
      },
    ];
    const message: UIMessage = {
      id: "r1:2",
      role: "assistant",
      parts: [
        { type: "step-start" },
        {
          type: "tool-search",
          toolCallId: "c-7",
          state: "output-available",
          input: { q: "plan" },
          output: "3 hits",
        },
      ],
    };
    render(
      <Conversation
        {...baseProps()}
        transcript={transcript}
        messages={[textMessage("r1:0", "user", "go"), message]}
      />,
    );

    expect(
      screen.getByText(
        (_, element) => element?.textContent === "The agent started a run of wf.lit-check.v5.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/search/)).toBeInTheDocument();
  });

  it("folds the earlier steps under the answer and expands them on click", async () => {
    const message: UIMessage = {
      id: "r1:5",
      role: "assistant",
      parts: [
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        {
          type: "tool-navigate",
          toolCallId: "c-1",
          state: "output-available",
          input: { url: "/chat" },
          output: "navigated to /chat",
        },
        { type: "step-start" },
        { type: "text", text: "Checking the tests." },
        { type: "step-start" },
        { type: "text", text: "All three read in full." },
      ],
    };
    render(<Conversation {...baseProps()} messages={[message]} />);

    expect(screen.getByText("All three read in full.")).toBeInTheDocument();
    expect(screen.getByText("Reading the plan.")).toBeInTheDocument();
    expect(screen.getByText("Checking the tests.")).toBeInTheDocument();
    expect(screen.getByText("navigate: navigated to /chat")).toBeInTheDocument();
    const fold = screen.getByRole("button", { name: "2 earlier steps" });
    expect(fold).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(fold);

    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Reading the plan.")).not.toBeInTheDocument();
    expect(screen.getByText("All three read in full.")).toBeInTheDocument();
  });

  it("opens the inspector from an expanded step", async () => {
    const onInspect = vi.fn();
    const message: UIMessage = {
      id: "r1:5",
      role: "assistant",
      parts: [
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        { type: "step-start" },
        { type: "text", text: "Done." },
      ],
    };
    render(<Conversation {...baseProps()} messages={[message]} onInspect={onInspect} />);

    await userEvent.click(screen.getByRole("button", { name: "Reading the plan." }));

    expect(onInspect).toHaveBeenCalledWith(message);
  });

  it("keeps every step in view, in order, while the turn streams, and folds once it settles", () => {
    const message: UIMessage = {
      id: "live",
      role: "assistant",
      parts: [
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        { type: "step-start" },
        { type: "text", text: "Reading the remaining halves." },
        { type: "step-start" },
      ],
    };
    const { rerender } = render(
      <Conversation {...baseProps()} messages={[message]} streaming={true} />,
    );

    expect(screen.getByText("Reading the plan.")).toBeInTheDocument();
    expect(screen.getByText("Reading the remaining halves.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "1 earlier step" })).not.toBeInTheDocument();

    rerender(<Conversation {...baseProps()} messages={[message]} streaming={false} />);

    expect(screen.getByText("Reading the plan.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 earlier step" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("folds the earlier steps once the turn settles when folding is preferred", () => {
    const message: UIMessage = {
      id: "live",
      role: "assistant",
      parts: [
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        { type: "step-start" },
        { type: "text", text: "Done." },
      ],
    };
    render(<Conversation {...baseProps()} messages={[message]} foldSettled />);

    expect(screen.queryByText("Reading the plan.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 earlier step" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("shows no fold on a turn that took a single step", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "assistant", "Drafted lit-check.")]}
      />,
    );

    expect(screen.queryByRole("button", { name: /step/ })).not.toBeInTheDocument();
  });

  it("never makes a user bubble clickable", () => {
    render(<Conversation {...baseProps()} messages={[textMessage("1", "user", "hello")]} />);
    expect(screen.queryByRole("button", { name: "View turn detail" })).not.toBeInTheDocument();
  });

  it("shows an info-box turn naming the workflow a stored tool call started, after that turn's own last bubble", () => {
    const transcript: TranscriptMessage[] = [
      { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: toolCallPayload("run_workflow", { workflow_id: "wf.lit-check.v5", arguments: {} }),
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "response",
        payload: { kind: "response", parts: [{ part_kind: "text", content: "Started it." }] },
        text: "Started it.",
      },
    ];
    render(
      <Conversation
        {...baseProps()}
        transcript={transcript}
        messages={[
          textMessage("r1:0", "user", "go"),
          textMessage("r1:2", "assistant", "Started it."),
        ]}
      />,
    );

    expect(
      screen.getByText(
        (_, element) => element?.textContent === "The agent started a run of wf.lit-check.v5.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the run" })).toHaveAttribute(
      "href",
      "/workflows/wf.lit-check.v5",
    );
  });

  it("shows no info-box turn when nothing called the run tool", () => {
    const transcript: TranscriptMessage[] = [
      { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("hi"), text: "hi" },
    ];
    render(
      <Conversation
        {...baseProps()}
        transcript={transcript}
        messages={[textMessage("r1:0", "user", "hi")]}
      />,
    );
    expect(screen.queryByRole("link", { name: "Open the run" })).not.toBeInTheDocument();
  });

  it("closes the transcript with the gate turn while this session's live run waits", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "start it")]}
        liveGate={{ pending: PENDING, startedAt: "2026-08-24T00:00:00.000Z", events: [] }}
      />,
    );

    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName.toLowerCase() === "p" &&
          element.textContent === "gate-1 is waiting for you.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("shows the gate turn instead of the thinking mark while a gate waits mid-stream", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "start it")]}
        streaming
        liveGate={{ pending: PENDING, startedAt: "2026-08-24T00:00:00.000Z", events: [] }}
      />,
    );

    expect(screen.queryByRole("status", { name: "Thinking" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("opens a placeholder bubble carrying the thinking mark before the turn's first part arrives", () => {
    render(<Conversation {...baseProps()} messages={[textMessage("1", "user", "hi")]} streaming />);

    expect(screen.getByRole("status", { name: "Thinking" })).toBeInTheDocument();
    expect(screen.getAllByText("Orchestrator agent")).toHaveLength(1);
    expect(screen.queryByText("thinking")).not.toBeInTheDocument();
  });

  it("keeps the thinking mark inside the live turn's own bubble, never a second bubble", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "hi"), textMessage("2", "assistant", "Reading.")]}
        streaming
      />,
    );

    expect(screen.getByRole("status", { name: "Thinking" })).toBeInTheDocument();
    expect(screen.getAllByText("Orchestrator agent")).toHaveLength(1);
  });

  it("fires onDecide from the gate turn's own panel", async () => {
    const onDecide = vi.fn();
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "start it")]}
        liveGate={{ pending: PENDING, startedAt: "2026-08-24T00:00:00.000Z", events: [] }}
        onDecide={onDecide}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledWith(true);
  });

  it("shows this session's own live run as a turn after the last message", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "start it")]}
        liveRun={{ title: "Literature check", run: runFixture(), graph: undefined }}
      />,
    );

    expect(screen.getByText("Run")).toBeInTheDocument();
    expect(screen.getByText("Literature check")).toBeInTheDocument();
    expect(screen.getByText("wf.lit-check.v5")).toBeInTheDocument();
  });

  it("shows no run turn when this session has no live run", () => {
    render(<Conversation {...baseProps()} messages={[textMessage("1", "user", "start it")]} />);

    expect(screen.queryByText("wf.lit-check.v5")).not.toBeInTheDocument();
  });

  it("puts the run turn before the gate turn when this session's live run waits", () => {
    render(
      <Conversation
        {...baseProps()}
        messages={[textMessage("1", "user", "start it")]}
        liveRun={{
          title: "Literature check",
          run: runFixture({ state: "waiting" }),
          graph: undefined,
        }}
        liveGate={{ pending: PENDING, startedAt: "2026-08-24T00:00:00.000Z", events: [] }}
      />,
    );

    const runTurnName = screen.getByText("Literature check");
    const gateSentence = screen.getByText(
      (_, element) =>
        element?.tagName.toLowerCase() === "p" &&
        element.textContent === "gate-1 is waiting for you.",
    );
    expect(
      runTurnName.compareDocumentPosition(gateSentence) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
