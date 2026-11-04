import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { MessagePayload } from "@/api/sessions-types";
import type { GraphNode } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { StepChatDrawer } from "./step-chat-drawer";

// The drawer's own network surface: GET /api/sessions/:id/run/steps (the item picker and the
// composer's executing gate) and GET .../run/steps/:node/stream (the live tail), mocked per
// test the same way step-chat.test.ts does. Archived tests hit no network at all -- the archive
// path reads straight off the `recordConversations` prop through `stepConversationToUiMessages`.

const BASE = "http://studio.test";

const NODE: GraphNode = {
  key: "n:draft",
  id: "draft",
  kind: "run",
  signature: "() -> str",
  detail: "",
  tools: [],
  required: true,
  is_output: false,
  unknown: false,
  brought: false,
  briefing: "Write the report",
  work: "draft",
  input_schemas: {},
  output_schema: null,
  source: "",
  turns: null,
};

const MAP_NODE: GraphNode = { ...NODE, kind: "map", id: "review", work: "review" };

// A spec that runs the same step under two node ids: the archive keys by the graph node id,
// never `work`, so this node's own key is `"draft[]"` even though its step is `compose_report`.
const NODE_SHARED_WORK: GraphNode = { ...NODE, work: "compose_report" };

function emptyStreamOf(node: string) {
  return http.get(`${BASE}/api/sessions/s-1/run/steps/${node}/stream`, () =>
    new HttpResponse("data: {\"type\":\"finish\"}\n\ndata: [DONE]\n\n", {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
}

// A stream opening with its own replay-floor frame, `floor`/`requested` set by the caller so a
// test can drive the dropped-output notice on or off, then the given chunks and a close.
function floorStreamOf(node: string, floor: number, requested: number, chunks: Array<Record<string, unknown>>) {
  const body =
    `data: ${JSON.stringify({ type: "data-replay-floor", data: { floor, requested } })}\n\n` +
    chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("") +
    "data: [DONE]\n\n";
  return http.get(`${BASE}/api/sessions/s-1/run/steps/${node}/stream`, () =>
    new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );
}

describe("StepChatDrawer, archived", () => {
  it("renders a user bubble and an assistant turn from a fixture conversation", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [
        { kind: "request", parts: [{ part_kind: "user-prompt", content: "write the report" }] },
        { kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] },
      ],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("write the report")).toBeInTheDocument();
    expect(screen.getByText("Here is the report.")).toBeInTheDocument();
  });

  it("looks the archive up by the node id, not by `work`, even when a spec runs the same step under two ids", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] }],
    };

    render(
      <StepChatDrawer
        sessionId={null}
        recordConversations={conversations}
        node={NODE_SHARED_WORK}
        open
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("Here is the report.")).toBeInTheDocument();
  });

  it("shows the step's own thinking, which the reused turn component alone would hide", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [
        { kind: "request", parts: [{ part_kind: "user-prompt", content: "go" }] },
        { kind: "response", parts: [{ part_kind: "thinking", content: "I should check the source first." }] },
      ],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("I should check the source first.")).toBeInTheDocument();
  });

  it("shows a refused answer's retry feedback as a refusal between the two turns, never as the step speaking", async () => {
    const refusal = "Invalid JSON: expected value at line 1 column 1";
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [
        { kind: "request", parts: [{ part_kind: "user-prompt", content: "go" }] },
        { kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] },
        {
          kind: "request",
          parts: [
            {
              part_kind: "retry-feedback",
              content: [{ type: "json_invalid", loc: [], msg: refusal, input: "Here is the report." }],
              retry: { cause: "validation_error" },
            },
          ],
        },
        { kind: "response", parts: [{ part_kind: "text", content: "The report, as asked." }] },
      ],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    const refusalText = await screen.findByText(refusal);
    expect(within(refusalText.parentElement as HTMLElement).getByText("Refused")).toBeInTheDocument();
    // "Step draft" names the drawer and each of the step's two turns, and nothing more.
    expect(screen.getAllByText("Step draft")).toHaveLength(3);
    expect(screen.getAllByText("You")).toHaveLength(1);
  });

  it("disables the composer with the finished notice (the archive is always read only)", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByText("Done.");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(
      screen.getByText("This step has finished. Its conversation is read only."),
    ).toBeInTheDocument();
  });

  it("shows an item picker for a node with more than one line, then that item's own conversation", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "review[item-0]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 0 done." }] }],
      "review[item-1]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 1 done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={MAP_NODE} open onClose={vi.fn()} />,
    );

    expect(screen.getByRole("button", { name: /item-0/ })).toBeInTheDocument();
    expect(screen.queryByText("Item 0 done.")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /item-0/ }));

    expect(await screen.findByText("Item 0 done.")).toBeInTheDocument();
  });

  it("returns to the item picker from a chosen item, and on to a different one", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "review[item-0]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 0 done." }] }],
      "review[item-1]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 1 done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={MAP_NODE} open onClose={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /item-0/ }));
    expect(await screen.findByText("Item 0 done.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Choose another item" }));

    expect(screen.getByRole("button", { name: /item-1/ })).toBeInTheDocument();
    expect(screen.queryByText("Item 0 done.")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /item-1/ }));
    expect(await screen.findByText("Item 1 done.")).toBeInTheDocument();
  });

  it("offers no Choose another item control for a node with only one line", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByText("Done.");
    expect(screen.queryByRole("button", { name: "Choose another item" })).not.toBeInTheDocument();
  });
});

const REFUSAL = "Invalid JSON: expected value at line 1 column 1";

// What the live stream carries for a step that answered in plain text, was refused and answered
// again: the two answers back to back, with nothing for the refusal between them.
const REFUSED_STREAM_CHUNKS = [
  { type: "start" },
  { type: "start-step" },
  { type: "text-start", id: "t-1" },
  { type: "text-delta", id: "t-1", delta: "Here is the report." },
  { type: "text-end", id: "t-1" },
  { type: "text-start", id: "t-2" },
  { type: "text-delta", id: "t-2", delta: "The report, as asked." },
  { type: "text-end", id: "t-2" },
  { type: "finish" },
];

// How the drawer draws that stream: one turn whose two answers run together.
const LIVE_RUN_TOGETHER = "Here is the report.The report, as asked.";

// What the same step recorded: the brief, the plain-text answer, the refusal that answered no
// call, and the answer it retried.
const REFUSED_RECORDING: MessagePayload[] = [
  { kind: "request", parts: [{ part_kind: "user-prompt", content: "write the report" }] },
  { kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] },
  {
    kind: "request",
    parts: [
      {
        part_kind: "retry-feedback",
        content: [{ type: "json_invalid", loc: [], msg: REFUSAL, input: "Here is the report." }],
        retry: { cause: "validation_error" },
      },
    ],
  },
  { kind: "response", parts: [{ part_kind: "text", content: "The report, as asked." }] },
];

// A stream that stays open after its chunks, as a live step's stream does until the run ends.
function openStreamOf(node: string, chunks: Array<Record<string, unknown>>) {
  return http.get(`${BASE}/api/sessions/s-1/run/steps/${node}/stream`, () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
        }
      },
    });
    return new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  });
}

function linesOf(executing: boolean) {
  return http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
    HttpResponse.json({ steps: [{ node: "draft", item: "", executing, settled: !executing }] }),
  );
}

describe("StepChatDrawer, live, once a line settles", () => {
  it("draws the recorded refusal of a settled line, which the live stream does not carry", async () => {
    let asked = "";
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, ({ request }) => {
        asked = new URL(request.url).search;
        return HttpResponse.json({ messages: REFUSED_RECORDING });
      }),
    );

    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);

    const refusalText = await screen.findByText(REFUSAL);
    expect(within(refusalText.parentElement as HTMLElement).getByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("write the report")).toBeInTheDocument();
    expect(asked).toBe("?item=");
  });

  it("keeps drawing the live stream for an executing line and reads no recording", async () => {
    let read = false;
    server.use(
      linesOf(true),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, () => {
        read = true;
        return HttpResponse.json({ messages: REFUSED_RECORDING });
      }),
    );

    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);

    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    expect(screen.queryByText("Refused")).not.toBeInTheDocument();
    expect(read).toBe(false);
  });

  it("keeps the live messages on screen until the recorded ones arrive", async () => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let asked = false;
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, async () => {
        asked = true;
        await held;
        return HttpResponse.json({ messages: REFUSED_RECORDING });
      }),
    );

    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);

    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    await waitFor(() => expect(asked).toBe(true));
    expect(screen.getByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    expect(screen.queryByText("Refused")).not.toBeInTheDocument();
    expect(screen.queryByText("No conversation recorded for this step.")).not.toBeInTheDocument();

    release();

    expect(await screen.findByText("Refused")).toBeInTheDocument();
    expect(screen.getByText("Here is the report.")).toBeInTheDocument();
    expect(screen.getByText("The report, as asked.")).toBeInTheDocument();
    expect(screen.queryByText(LIVE_RUN_TOGETHER)).not.toBeInTheDocument();
  });

  it("goes back to the live stream while the line runs again, and draws the new recording once it settles", async () => {
    let executing = false;
    let settledAgain = false;
    let readsAfterSettlingAgain = 0;
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const secondRecording: MessagePayload[] = [
      ...REFUSED_RECORDING,
      { kind: "request", parts: [{ part_kind: "user-prompt", content: "now the summary" }] },
      { kind: "response", parts: [{ part_kind: "text", content: "The summary." }] },
    ];
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing, settled: !executing }] }),
      ),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, async () => {
        if (!settledAgain) {
          return HttpResponse.json({ messages: REFUSED_RECORDING });
        }
        readsAfterSettlingAgain += 1;
        await held;
        return HttpResponse.json({ messages: secondRecording });
      }),
    );

    const { queryClient } = render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("Refused")).toBeInTheDocument();

    executing = true;
    await queryClient.invalidateQueries({ queryKey: ["run"] });
    await waitFor(() => expect(screen.queryByText("Refused")).not.toBeInTheDocument());
    expect(screen.getByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();

    settledAgain = true;
    executing = false;
    await queryClient.invalidateQueries({ queryKey: ["run"] });
    await waitFor(() => expect(readsAfterSettlingAgain).toBe(1));
    // The first recording is not drawn again while the second is on its way.
    expect(screen.queryByText("Refused")).not.toBeInTheDocument();
    expect(screen.getByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();

    release();

    expect(await screen.findByText("The summary.")).toBeInTheDocument();
    expect(screen.getByText("Refused")).toBeInTheDocument();
  });

  it("never shows an earlier recording when the drawer is opened again on a line that ran since", async () => {
    let reopened = false;
    let readsAfterReopening = 0;
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const laterRecording: MessagePayload[] = [
      { kind: "request", parts: [{ part_kind: "user-prompt", content: "write the report" }] },
      { kind: "response", parts: [{ part_kind: "text", content: "The summary." }] },
    ];
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, async () => {
        if (!reopened) {
          return HttpResponse.json({ messages: REFUSED_RECORDING });
        }
        readsAfterReopening += 1;
        await held;
        return HttpResponse.json({ messages: laterRecording });
      }),
    );
    const drawer = <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />;

    const { rerender } = render(drawer);
    expect(await screen.findByText("Refused")).toBeInTheDocument();

    // Closed, as the page closes it: the drawer leaves the tree, and a later click mounts a new one.
    rerender(<div />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    reopened = true;
    rerender(drawer);

    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    await waitFor(() => expect(readsAfterReopening).toBe(1));
    expect(screen.queryByText("Refused")).not.toBeInTheDocument();

    release();

    expect(await screen.findByText("The summary.")).toBeInTheDocument();
    expect(screen.queryByText("Refused")).not.toBeInTheDocument();
  });

  it("keeps the live messages and says so when the recording cannot be read", async () => {
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, () =>
        HttpResponse.json({ ok: false, message: "Not Found" }, { status: 404 }),
      ),
    );

    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);

    expect(await screen.findByText("Could not read what this step recorded.")).toBeInTheDocument();
    expect(screen.getByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
  });

  it("keeps the live messages when no recorded message can be read", async () => {
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, () =>
        HttpResponse.json({ messages: [{ parts: "not a message" }] }),
      ),
    );

    const { queryClient } = render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        queryClient
          .getQueryCache()
          .findAll({ queryKey: ["run", "steps", "s-1", "messages"] })
          .some((query) => query.state.status === "success"),
      ).toBe(true),
    );
    expect(screen.getByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();
    expect(screen.queryByText("No conversation recorded for this step.")).not.toBeInTheDocument();
  });

  it("keeps the reader's distance from the bottom when the recording replaces the live messages", async () => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      linesOf(false),
      openStreamOf("draft", REFUSED_STREAM_CHUNKS),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, async () => {
        await held;
        return HttpResponse.json({ messages: REFUSED_RECORDING });
      }),
    );

    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);
    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();

    // jsdom lays nothing out, so the log's heights are given: the recording, with the brief above
    // the answers and the refusal between them, stands 300px taller than the live messages.
    const log = screen.getByRole("log", { name: "Step conversation" });
    Object.defineProperty(log, "scrollHeight", {
      configurable: true,
      get: () => (log.textContent?.includes("Refused") ? 1300 : 1000),
    });
    Object.defineProperty(log, "scrollTop", { configurable: true, writable: true, value: 0 });
    log.scrollTop = 600;
    fireEvent.scroll(log);

    release();

    expect(await screen.findByText("Refused")).toBeInTheDocument();
    expect(log.scrollTop).toBe(900);
  });

  it("leaves a reader who scrolled up where they are while the source stays the same", async () => {
    server.use(linesOf(true), openStreamOf("draft", REFUSED_STREAM_CHUNKS));
    const onClose = vi.fn();
    const { rerender } = render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={onClose} />);
    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();

    const log = screen.getByRole("log", { name: "Step conversation" });
    let height = 1000;
    Object.defineProperty(log, "scrollHeight", { configurable: true, get: () => height });
    Object.defineProperty(log, "scrollTop", { configurable: true, writable: true, value: 0 });
    log.scrollTop = 200;
    fireEvent.scroll(log);

    // A new node object with the same id renders the conversation again with its source unchanged.
    height = 1300;
    rerender(<StepChatDrawer sessionId="s-1" recordConversations={null} node={{ ...NODE }} open onClose={onClose} />);

    expect(log.scrollTop).toBe(200);
  });

  it("marks the step conversation so a screen reader does not announce it as it streams", async () => {
    server.use(linesOf(true), openStreamOf("draft", REFUSED_STREAM_CHUNKS));
    render(<StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />);
    expect(await screen.findByText(LIVE_RUN_TOGETHER)).toBeInTheDocument();

    expect(screen.getByRole("log", { name: "Step conversation" })).toHaveAttribute("aria-live", "off");
  });
});

describe("StepChatDrawer, live", () => {
  it("disables the composer with the finished notice when no line is executing", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: false, settled: true }] }),
      ),
      emptyStreamOf("draft"),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/messages`, () =>
        HttpResponse.json({ messages: [{ kind: "response", parts: [{ part_kind: "text", content: "Done." }] }] }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(
      screen.getByText("This step has finished. Its conversation is read only."),
    ).toBeInTheDocument();
  });

  it("sends a steer with the session, node, item, and typed text", async () => {
    let hitPath = "";
    let body: unknown;
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, async ({ request }) => {
        hitPath = new URL(request.url).pathname + new URL(request.url).search;
        body = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await userEvent.type(textbox, "slow down");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(hitPath).toBe("/api/sessions/s-1/run/steps/draft/steer?item="));
    expect(body).toEqual({ message: "slow down" });
  });

  it("surfaces a 409 steer refusal as the composer notice", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, () =>
        HttpResponse.json({ ok: false, message: "this step has already finished" }, { status: 409 }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await userEvent.type(textbox, "slow down");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("this step has already finished")).toBeInTheDocument();
  });

  it("disables the composer with the not-started notice when the node has no line yet", async () => {
    server.use(http.get(`${BASE}/api/sessions/s-1/run/steps`, () => HttpResponse.json({ steps: [] })));

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(screen.getByText("This step has not started yet.")).toBeInTheDocument();
  });

  it("disables the composer with the could-not-read notice when the lines fetch fails", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ ok: false, message: "boom" }, { status: 500 }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(screen.getByText("Could not read this step.")).toBeInTheDocument();
  });

  it("labels the bubble with the step, not the orchestrator, and offers no turn-detail button", async () => {
    const chunks = [
      { type: "start" },
      { type: "text-start", id: "t-1" },
      { type: "text-delta", id: "t-1", delta: "Working." },
      { type: "text-end", id: "t-1" },
      { type: "finish" },
    ];
    const body = `${chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("")}data: [DONE]\n\n`;
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () =>
        new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("Working.")).toBeInTheDocument();
    // "Step draft" names both the drawer (its own title) and the bubble (the speaker label) --
    // the second occurrence is the one this test is about.
    expect(screen.getAllByText("Step draft")).toHaveLength(2);
    expect(screen.queryByText("Orchestrator agent")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View turn detail" })).not.toBeInTheDocument();
  });

  it("closes an unmatched bracket in the thinking and the bubble only while the step streams", async () => {
    const encoder = new TextEncoder();
    const held: { stream: ReadableStreamDefaultController<Uint8Array> | null } = { stream: null };
    const send = (chunk: Record<string, unknown> | "[DONE]"): void => {
      const data = chunk === "[DONE]" ? chunk : JSON.stringify(chunk);
      held.stream?.enqueue(encoder.encode(`data: ${data}\n\n`));
    };
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            held.stream = controller;
          },
        });
        return new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }),
    );
    const placeholders = (): number => document.body.innerHTML.split("streamdown:incomplete-link").length - 1;

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );
    await waitFor(() => expect(held.stream).not.toBeNull());
    send({ type: "start" });
    send({ type: "reasoning-start", id: "r-1" });
    send({ type: "reasoning-delta", id: "r-1", delta: "Check [the source" });
    send({ type: "reasoning-end", id: "r-1" });
    send({ type: "text-start", id: "t-1" });
    send({ type: "text-delta", id: "t-1", delta: "Read [the plan" });

    await waitFor(() => expect(placeholders()).toBe(2));

    send({ type: "text-end", id: "t-1" });
    send({ type: "finish" });
    send("[DONE]");
    held.stream?.close();

    await waitFor(() => expect(placeholders()).toBe(0));
    expect(document.body.textContent).toContain("Check [the source");
    expect(document.body.textContent).toContain("Read [the plan");
  });

  it("shows no gate-mode caption -- the drawer steers a step, not a run", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByText("gates wait for you")).not.toBeInTheDocument();
  });

  it("shows the dropped-output notice above the transcript when the floor is past what it asked for", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      floorStreamOf("draft", 40, 0, [
        { type: "text-start", id: "t-1" },
        { type: "text-delta", id: "t-1", delta: "Working." },
        { type: "text-end", id: "t-1" },
        { type: "finish" },
      ]),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(
      await screen.findByText(
        "Earlier output was dropped from the live buffer. The full conversation is archived when the run ends.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no dropped-output notice when the floor matches what it asked for", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      floorStreamOf("draft", 0, 0, [{ type: "finish" }]),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByText(/Earlier output was dropped/)).not.toBeInTheDocument();
  });
});
