import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { type ReactNode, StrictMode, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/shared/test-support/msw/server";

import { stepUsages } from "./step-usage";
import { buildChatBody, useSessionChat } from "./use-session-chat";

import type { UIMessage } from "ai";

// The chat engine's own network surface: POST /api/sessions/:id/messages (the
// Vercel AI data-stream protocol, one `data:` line per chunk, closed by `data:
// [DONE]`) and POST /api/sessions/:id/interrupt (the sessions API client). Every
// test owns both, mirroring the rest of the suite's convention.

const BASE = "http://studio.test";

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return `${lines}data: [DONE]\n\n`;
}

function streamResponse(chunks: StreamChunk[] = [{ type: "start" }, { type: "finish" }]) {
  return new HttpResponse(sseBody(chunks), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

// The user message a send put on the wire, as the hook itself holds it. The SDK mints that
// message's own id, so reading it back off the hook is what lets a request body be asserted
// whole rather than in part.
function sentMessages(messages: UIMessage[]): UIMessage[] {
  const typed = messages.filter((message) => message.role === "user").at(-1);
  return typed === undefined ? [] : [typed];
}

function wrapperFor(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

// Strict mode mounts twice and re-runs every effect, so anything the engine performs
// once has to survive being asked for twice.
function strictWrapper() {
  const Inner = makeWrapper();
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(StrictMode, null, createElement(Inner, null, children));
  };
}

function makeWrapper() {
  return wrapperFor(new QueryClient({ defaultOptions: { queries: { retry: false } } }));
}

// Every send knocks on the steer endpoint first, and an idle session answers 409 before the
// message goes out as an ordinary turn. Tests that exercise a BUSY session register their own
// steer handler in the test body, which takes precedence over this default.
beforeEach(() => {
  server.use(
    http.post(`${BASE}/api/sessions/:id/steer`, () =>
      HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
    ),
  );
});

describe("useSessionChat", () => {
  it("sends only the latest user text on the wire, to this session's own stream", async () => {
    const bodies: unknown[] = [];
    let hitPath = "";
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, async ({ request }) => {
        hitPath = new URL(request.url).pathname;
        bodies.push(await request.json());
        return streamResponse();
      }),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("draft me a check");
    });

    expect(bodies.at(-1)).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages: sentMessages(result.current.messages),
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "draft me a check" },
    ]);
    expect(hitPath).toBe("/api/sessions/s1/messages");
  });

  it("stop aborts the local stream and interrupts the session on the server", async () => {
    let interruptCalls = 0;
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => streamResponse()),
      http.post(`${BASE}/api/sessions/s1/interrupt`, () => {
        interruptCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      result.current.stop();
    });

    expect(interruptCalls).toBe(1);
  });

  it("a send while the turn streams steers it instead of starting a second turn", async () => {
    const encoder = new TextEncoder();
    const held = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "start" })}\n\n`));
      },
    });
    // The server's own view: idle until the stream opens, busy from then on.
    let streamCalls = 0;
    const steered: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => {
        streamCalls += 1;
        return new HttpResponse(held, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }),
      http.post(`${BASE}/api/sessions/s1/steer`, async ({ request }) => {
        if (streamCalls === 0) {
          return HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 });
        }
        steered.push(await request.json());
        return HttpResponse.json({ ok: true });
      }),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      void result.current.send("start the digest");
      await Promise.resolve();
    });
    await waitFor(() => expect(["submitted", "streaming"]).toContain(result.current.status));

    await act(async () => {
      await result.current.send("also check the tests");
    });

    expect(steered).toEqual([{ message: "also check the tests" }]);
    expect(streamCalls).toBe(1);
    const last = result.current.messages.at(-1);
    expect(last?.role).toBe("user");
    expect(last?.parts).toEqual([{ type: "text", text: "also check the tests" }]);
  });

  it("steer called directly reaches the endpoint with no local stream at all", async () => {
    const steered: unknown[] = [];
    let streamCalls = 0;
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => {
        streamCalls += 1;
        return streamResponse();
      }),
      http.post(`${BASE}/api/sessions/s1/steer`, async ({ request }) => {
        steered.push(await request.json());
        return HttpResponse.json({ ok: true });
      }),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.steer("redirect the run");
    });

    expect(steered).toEqual([{ message: "redirect the run" }]);
    expect(streamCalls).toBe(0);
    expect(result.current.messages.at(-1)?.role).toBe("user");
  });

  it("a steer refused because the turn settled goes out as its own turn", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, async ({ request }) => {
        bodies.push(await request.json());
        return streamResponse();
      }),
      http.post(`${BASE}/api/sessions/s1/steer`, () =>
        HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("first");
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.send("second");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages: sentMessages(result.current.messages),
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "second" },
    ]);
  });

  it("surfaces an interrupt failure as the error", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => streamResponse()),
      http.post(`${BASE}/api/sessions/s1/interrupt`, () =>
        HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
      ),
    );

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "s1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    act(() => {
      result.current.stop();
    });

    await waitFor(() =>
      expect(result.current.error).toContain("nothing is in flight in this session"),
    );
  });
});

describe("buildChatBody", () => {
  const messages: UIMessage[] = [
    { id: "u1", role: "user", parts: [{ type: "text", text: "hello" }] },
  ];

  it("is the submit-message shape carrying the new message alone when no context is supplied", () => {
    expect(buildChatBody("s1", messages, null, new Set())).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages,
    });
  });
  it("says so when the surface only designs, and says nothing otherwise", () => {
    expect(buildChatBody("s1", messages, null, new Set(), true)).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages,
      design_only: true,
    });
    expect(buildChatBody("s1", messages, null, new Set(), false)).not.toHaveProperty("design_only");
  });
  it("carries the context beside the message when one is supplied", () => {
    const context = {
      url: "/chat",
      page: "chat",
      ids: {},
      tab: "",
      trail: [],
      extras: {},
      targets: [],
    };
    expect(buildChatBody("s1", messages, context, new Set())).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages,
      context,
    });
  });

  it("sends no message at all and the tool results on a continuation", () => {
    const messages: UIMessage[] = [
      { id: "u1", role: "user", parts: [{ type: "text", text: "go" }] },
      {
        id: "a1",
        role: "assistant",
        parts: [
          {
            type: "tool-navigate",
            toolCallId: "c-1",
            state: "output-available",
            input: { url: "/chat" },
            output: "navigated to /chat",
          },
        ],
      },
    ];
    expect(buildChatBody("s1", messages, null, new Set(["c-1"]))).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages: [],
      tool_results: [{ tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /chat" }],
    });
  });

  it("sends the typed text beside the denials it superseded", () => {
    const messages: UIMessage[] = [
      {
        id: "a1",
        role: "assistant",
        parts: [
          {
            type: "tool-toggle_plugin",
            toolCallId: "c-2",
            state: "approval-responded",
            input: {},
            approval: { id: "c-2", approved: false, reason: "superseded" },
          },
        ],
      },
      { id: "u2", role: "user", parts: [{ type: "text", text: "actually, no" }] },
    ];
    expect(buildChatBody("s1", messages, null, new Set(["c-2"]))).toEqual({
      trigger: "submit-message",
      id: "s1",
      messages: [messages[1]],
      tool_results: [
        { tool_call_id: "c-2", tool_name: "toggle_plugin", approved: false, reason: "superseded" },
      ],
    });
  });
});

describe("useSessionChat settle", () => {
  it("takes a data-step that arrives after the settle, the order the server streams", async () => {
    // The live frame order on a one-step turn: the run result dispatches the settle callback
    // before the handler that drains the step readings, so `data-step` lands after `data-settle`.
    // Nothing here may stop reading at the settle.
    const settleThenStep: StreamChunk[] = [
      { type: "start" },
      { type: "start-step" },
      { type: "text-start", id: "t1" },
      { type: "text-delta", id: "t1", delta: "done" },
      { type: "text-end", id: "t1" },
      { type: "data-settle", data: SETTLE_PAYLOAD },
      {
        type: "data-step",
        data: {
          tool_call_ids: ["c-1"],
          usage: {
            input_tokens: 11,
            output_tokens: 7,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
          },
          timestamp: "2026-09-07T12:00:00+00:00",
        },
      },
      { type: "finish-step" },
      { type: "finish" },
    ];
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, () => streamResponse(settleThenStep)),
    );
    const onSettle = vi.fn();

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("go");
    });

    expect(onSettle).toHaveBeenCalledTimes(1);
    expect(result.current.messages.flatMap(stepUsages)).toEqual([
      {
        tool_call_ids: ["c-1"],
        usage: {
          input_tokens: 11,
          output_tokens: 7,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
        },
        timestamp: "2026-09-07T12:00:00+00:00",
      },
    ]);
  });


  it("renders the turn and calls no settle when a data-settle part fails the reply contract", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const broken: StreamChunk[] = [
      { type: "start" },
      { type: "start-step" },
      { type: "text-start", id: "t1" },
      { type: "text-delta", id: "t1", delta: "done" },
      { type: "text-end", id: "t1" },
      {
        type: "data-settle",
        data: {
          message: "done",
          draft: { spec_yaml: "", title: "", summary: "", applicability: "", lineage: null },
          issues: [],
          graph: null,
        },
      },
      { type: "finish-step" },
      { type: "finish" },
    ];
    server.use(http.post(`${BASE}/api/sessions/m-1/messages`, () => streamResponse(broken)));
    const onSettle = vi.fn();

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("go");
    });

    expect(onSettle).not.toHaveBeenCalled();
    expect(result.current.status).toBe("ready");
    expect(result.current.messages.at(-1)?.role).toBe("assistant");
    expect(errorSpy).toHaveBeenCalledOnce();
    errorSpy.mockRestore();
  });
});

// A deferred turn: the stream ends with companion tool calls the browser owes an answer
// for. A verb is performed here and reported back, a console act waits for the person.

const SETTLE_PAYLOAD = {
  message: "done",
  draft: { spec_yaml: "", title: "", summary: "", applicability: "", lineage: null },
  issues: [],
  valid: false,
  graph: null,
};

const DEFERRED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-1", toolName: "navigate" },
  {
    type: "tool-input-available",
    toolCallId: "c-1",
    toolName: "navigate",
    input: { url: "/chat" },
  },
  { type: "finish-step" },
  { type: "finish" },
];

const APPROVAL: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-2", toolName: "toggle_plugin" },
  {
    type: "tool-input-available",
    toolCallId: "c-2",
    toolName: "toggle_plugin",
    input: { name: "sql", on: true },
  },
  { type: "tool-approval-request", approvalId: "c-2", toolCallId: "c-2" },
  { type: "finish-step" },
  { type: "finish" },
];

const SETTLED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "text-start", id: "t1" },
  { type: "text-delta", id: "t1", delta: "done" },
  { type: "text-end", id: "t1" },
  { type: "data-settle", data: SETTLE_PAYLOAD },
  { type: "finish-step" },
  { type: "finish" },
];

// The shape the adapter really streams: it closes the step and opens a new one before
// every model response after the first, so an act's output sits a step behind the
// closing text.
const OUTCOME: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-available", toolCallId: "c-2", toolName: "toggle_plugin", input: {} },
  { type: "tool-output-available", toolCallId: "c-2", output: "Plugin sql is on." },
  { type: "finish-step" },
  ...SETTLED.slice(1),
];

// Two acts announced in one step, so the person can answer one and leave the other.
const TWO_ACTS: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-2", toolName: "toggle_plugin" },
  {
    type: "tool-input-available",
    toolCallId: "c-2",
    toolName: "toggle_plugin",
    input: { name: "sql", on: true },
  },
  { type: "tool-approval-request", approvalId: "c-2", toolCallId: "c-2" },
  { type: "tool-input-start", toolCallId: "c-3", toolName: "toggle_plugin" },
  {
    type: "tool-input-available",
    toolCallId: "c-3",
    toolName: "toggle_plugin",
    input: { name: "shell", on: true },
  },
  { type: "tool-approval-request", approvalId: "c-3", toolCallId: "c-3" },
  { type: "finish-step" },
  { type: "finish" },
];

// A step whose second call never finished announcing its arguments, the shape an
// interrupted run leaves behind.
const HALF_ANNOUNCED: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-1", toolName: "navigate" },
  {
    type: "tool-input-available",
    toolCallId: "c-1",
    toolName: "navigate",
    input: { url: "/chat" },
  },
  { type: "tool-input-start", toolCallId: "c-4", toolName: "toggle_plugin" },
  { type: "tool-input-delta", toolCallId: "c-4", inputTextDelta: '{"name":' },
  { type: "finish-step" },
  { type: "finish" },
];

// A response that announces its chunks and then stays open, the way a turn still
// running on the server does.
function hangingResponse(chunks: StreamChunk[]) {
  const encoder = new TextEncoder();
  return new HttpResponse(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
        }
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

// Long enough for a late request to land, so a count asserted after it means the engine
// really did stop rather than that the test looked too early.
async function quietPeriod(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
  });
}

function recordTwoStreams(first: StreamChunk[], second: StreamChunk[]) {
  const bodies: unknown[] = [];
  let calls = 0;
  server.use(
    http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
      bodies.push(await request.json());
      calls += 1;
      return streamResponse(calls === 1 ? first : second);
    }),
  );
  return bodies;
}

describe("useSessionChat deferred turns", () => {
  it("executes a verb when the stream ends deferred and sends the continuation with its outcome", async () => {
    const bodies = recordTwoStreams(DEFERRED, SETTLED);
    const executeTool = vi.fn((name: string) =>
      name === "navigate" ? "navigated to /chat" : null,
    );

    const { result } = renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: [],
          onSettle: vi.fn(),
          executeTool,
        }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("take me to chat");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(executeTool).toHaveBeenCalledWith("navigate", { url: "/chat" });
    expect(executeTool).toHaveBeenCalledTimes(1);
    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: [],
      tool_results: [{ tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /chat" }],
    });
  });

  it("performs a verb once and continues once, however many renders follow", async () => {
    const bodies = recordTwoStreams(DEFERRED, SETTLED);
    const executeTool = vi.fn(() => "navigated to /chat");

    const { result, rerender } = renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: [],
          onSettle: vi.fn(),
          executeTool,
        }),
      { wrapper: strictWrapper() },
    );

    await act(async () => {
      await result.current.send("take me to chat");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));
    for (let round = 0; round < 5; round += 1) {
      await act(async () => {
        rerender();
      });
    }

    expect(executeTool).toHaveBeenCalledTimes(1);
    expect(bodies).toHaveLength(2);
  });

  it("holds an approval request until the person answers, then continues", async () => {
    const bodies = recordTwoStreams(APPROVAL, SETTLED);

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("open sql");
    });
    await waitFor(() =>
      expect(result.current.pendingApprovals).toEqual([
        {
          approvalId: "c-2",
          toolCallId: "c-2",
          toolName: "toggle_plugin",
          input: { name: "sql", on: true },
        },
      ]),
    );
    expect(bodies).toHaveLength(1);

    await act(async () => {
      await result.current.respond("c-2", true);
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: [],
      tool_results: [{ tool_call_id: "c-2", tool_name: "toggle_plugin", approved: true }],
    });
    expect(result.current.pendingApprovals).toEqual([]);
  });

  it("denies pending approvals as superseded when the person types instead", async () => {
    const bodies = recordTwoStreams(APPROVAL, SETTLED);

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("open sql");
    });
    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    await act(async () => {
      await result.current.send("never mind");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: sentMessages(result.current.messages),
      tool_results: [
        { tool_call_id: "c-2", tool_name: "toggle_plugin", approved: false, reason: "superseded" },
      ],
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "never mind" },
    ]);
    await quietPeriod();
    expect(bodies).toHaveLength(2);
  });

  it("sends one request when the person answers one act of a step and types over the rest", async () => {
    const bodies = recordTwoStreams(TWO_ACTS, SETTLED);

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("open sql and shell");
    });
    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(2));

    await act(async () => {
      await result.current.respond("c-2", true);
    });
    await quietPeriod();
    expect(bodies).toHaveLength(1);

    await act(async () => {
      await result.current.send("never mind");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));
    await quietPeriod();

    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: sentMessages(result.current.messages),
      tool_results: [
        { tool_call_id: "c-2", tool_name: "toggle_plugin", approved: true },
        { tool_call_id: "c-3", tool_name: "toggle_plugin", approved: false, reason: "superseded" },
      ],
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "never mind" },
    ]);
  });

  it("starts nothing when the person hands the composer back with an act already answered", async () => {
    const bodies = recordTwoStreams(TWO_ACTS, SETTLED);

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("open sql and shell");
    });
    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(2));

    await act(async () => {
      await result.current.respond("c-2", true);
    });
    await quietPeriod();

    // Reply instead: the person means to type, so the turn parks rather than continuing
    // on its own with an answer they have not finished giving.
    await act(async () => {
      await result.current.denyPending("superseded");
    });
    await quietPeriod();
    expect(bodies).toHaveLength(1);

    // What they type carries both answers, read off the messages rather than off a tally.
    await act(async () => {
      await result.current.send("do neither");
    });
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: sentMessages(result.current.messages),
      tool_results: [
        { tool_call_id: "c-2", tool_name: "toggle_plugin", approved: true },
        { tool_call_id: "c-3", tool_name: "toggle_plugin", approved: false, reason: "superseded" },
      ],
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "do neither" },
    ]);
  });

  it("performs nothing and starts nothing once the person stops the turn", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
        bodies.push(await request.json());
        return hangingResponse(DEFERRED.slice(0, 4));
      }),
      http.post(`${BASE}/api/sessions/m-1/interrupt`, () => HttpResponse.json({ ok: true })),
    );
    const executeTool = vi.fn(() => "navigated to /chat");

    const { result } = renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: [],
          onSettle: vi.fn(),
          executeTool,
        }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      void result.current.send("take me to chat");
      await Promise.resolve();
    });
    await waitFor(() => expect(result.current.status).toBe("streaming"));

    await act(async () => {
      result.current.stop();
    });
    await quietPeriod();

    expect(executeTool).not.toHaveBeenCalled();
    expect(bodies).toHaveLength(1);
  });

  it("holds the step while a call is still announcing its arguments", async () => {
    const bodies = recordTwoStreams(HALF_ANNOUNCED, SETTLED);
    const executeTool = vi.fn(() => "navigated to /chat");

    const { result } = renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: [],
          onSettle: vi.fn(),
          executeTool,
        }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("take me to chat");
    });
    await waitFor(() => expect(executeTool).toHaveBeenCalledTimes(1));
    await quietPeriod();

    expect(bodies).toHaveLength(1);
  });

  it("resumes the turn when the answer arrives from a stale render", async () => {
    const bodies = recordTwoStreams(APPROVAL, SETTLED);

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: makeWrapper() },
    );
    const staleRespond = result.current.respond;

    await act(async () => {
      await result.current.send("open sql");
    });
    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    await act(async () => {
      await staleRespond("c-2", true);
    });
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(bodies[1]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: [],
      tool_results: [{ tool_call_id: "c-2", tool_name: "toggle_plugin", approved: true }],
    });
  });

  it("forgets what it settled when the viewed session changes", async () => {
    const first: unknown[] = [];
    const second: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
        first.push(await request.json());
        return streamResponse(first.length === 1 ? DEFERRED : SETTLED);
      }),
      http.post(`${BASE}/api/sessions/m-2/messages`, async ({ request }) => {
        second.push(await request.json());
        return streamResponse(second.length === 1 ? DEFERRED : SETTLED);
      }),
    );
    const executeTool = vi.fn(() => "navigated to /chat");

    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string }) =>
        useSessionChat({ sessionId, initialMessages: [], onSettle: vi.fn(), executeTool }),
      { wrapper: makeWrapper(), initialProps: { sessionId: "m-1" } },
    );

    await act(async () => {
      await result.current.send("take me to chat");
    });
    await waitFor(() => expect(first).toHaveLength(2));

    rerender({ sessionId: "m-2" });
    await act(async () => {
      await result.current.send("take me to chat");
    });
    await waitFor(() => expect(second).toHaveLength(2));

    expect(executeTool).toHaveBeenCalledTimes(2);
  });

  it("invalidates every query once an act has an outcome", async () => {
    server.use(http.post(`${BASE}/api/sessions/m-1/messages`, () => streamResponse(OUTCOME)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle: vi.fn() }),
      { wrapper: wrapperFor(client) },
    );

    await act(async () => {
      await result.current.send("open sql");
    });

    await waitFor(() => expect(spy).toHaveBeenCalledWith());
  });

  it("leaves a read-back transcript alone: an outcome this browser never produced never continues", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
        bodies.push(await request.json());
        return streamResponse(SETTLED);
      }),
    );
    // The shape a re-pin really reads back: one bubble per transcript row, its companion calls
    // as output-available parts, and no step boundary anywhere in it.
    const rehydrated: UIMessage[] = [
      { id: "u1", role: "user", parts: [{ type: "text", text: "open sql" }] },
      {
        id: "a1",
        role: "assistant",
        parts: [
          {
            type: "tool-navigate",
            toolCallId: "c-9",
            state: "output-available",
            input: { url: "/chat" },
            output: "navigated to /chat",
          },
        ],
      },
    ];

    renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: rehydrated,
          onSettle: vi.fn(),
          executeTool: vi.fn(() => "navigated to /chat"),
        }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(bodies).toHaveLength(0);
  });

  it("hands the caller the last settle of a turn, never an earlier one", async () => {
    // A deferred end settles the turn once with nothing to show, and the continuation settles it
    // again with the answer. Both parts land in the one assistant message.
    const twoSettles: StreamChunk[] = [
      { type: "start" },
      { type: "start-step" },
      { type: "data-settle", data: { ...SETTLE_PAYLOAD, message: "" } },
      { type: "text-start", id: "t1" },
      { type: "text-delta", id: "t1", delta: "here it is" },
      { type: "text-end", id: "t1" },
      { type: "data-settle", data: { ...SETTLE_PAYLOAD, message: "here it is" } },
      { type: "finish-step" },
      { type: "finish" },
    ];
    server.use(http.post(`${BASE}/api/sessions/m-1/messages`, () => streamResponse(twoSettles)));
    const onSettle = vi.fn();

    const { result } = renderHook(
      () => useSessionChat({ sessionId: "m-1", initialMessages: [], onSettle }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("take me to chat");
    });

    expect(onSettle).toHaveBeenCalledTimes(1);
    expect(onSettle.mock.calls[0]?.[0]).toMatchObject({ message: "here it is" });
  });

  it("reports nothing back for a call this browser never answered when the person types", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/m-1/messages`, async ({ request }) => {
        bodies.push(await request.json());
        return streamResponse(SETTLED);
      }),
    );
    // A companion call whose continuation never landed before the reader left: the transcript
    // has no return for it, so the read-back bubble marks it pending.
    const rehydrated: UIMessage[] = [
      { id: "u1", role: "user", parts: [{ type: "text", text: "take me to chat" }] },
      {
        id: "a1",
        role: "assistant",
        parts: [
          {
            type: "tool-navigate",
            toolCallId: "c-1",
            state: "output-available",
            input: { url: "/chat" },
            output: "pending",
          },
        ],
      },
    ];

    const { result } = renderHook(
      () =>
        useSessionChat({
          sessionId: "m-1",
          initialMessages: rehydrated,
          onSettle: vi.fn(),
          executeTool: vi.fn(() => "navigated to /chat"),
        }),
      { wrapper: makeWrapper() },
    );

    await act(async () => {
      await result.current.send("what now");
    });

    expect(bodies).toHaveLength(1);
    // Exact: a stray tool_results for the call this browser never answered would fail here.
    expect(bodies[0]).toEqual({
      trigger: "submit-message",
      id: "m-1",
      messages: sentMessages(result.current.messages),
    });
    expect(sentMessages(result.current.messages)[0]?.parts).toEqual([
      { type: "text", text: "what now" },
    ]);
  });
});
