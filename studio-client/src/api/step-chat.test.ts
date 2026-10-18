import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";

import { StepNotSteerableError, stepChatApi, stepConversationToUiMessages, useStepStream } from "./step-chat";

import type { MessagePayload } from "./sessions-types";

// The step chat's own network surface: GET /api/sessions/:id/run/steps (the line listing),
// POST .../run/steps/:node/steer (409 typed as StepNotSteerableError), and GET
// .../run/steps/:node/stream (the replayable SSE tail). Each test owns its own handlers,
// mirroring the rest of the suite's convention.

const BASE = "http://studio.test";

describe("stepChatApi.lines", () => {
  it("zod-parses the steps envelope into the line list", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({
          steps: [
            { node: "draft", item: "", executing: true, settled: false },
            { node: "review", item: "item-0", executing: false, settled: true },
          ],
        }),
      ),
    );

    const lines = await stepChatApi.lines("s-1");

    expect(lines).toEqual([
      { node: "draft", item: "", executing: true, settled: false },
      { node: "review", item: "item-0", executing: false, settled: true },
    ]);
  });

  it("throws a ZodError on a response missing a field the schema requires", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true }] }),
      ),
    );

    await expect(stepChatApi.lines("s-1")).rejects.toThrow();
  });
});

describe("stepChatApi.steer", () => {
  it("posts the message to the node's steer route with the item as a query param", async () => {
    let hitPath = "";
    let body: unknown;
    server.use(
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, async ({ request }) => {
        hitPath = new URL(request.url).pathname + new URL(request.url).search;
        body = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    await stepChatApi.steer("s-1", "draft", "item-0", "slow down");

    expect(hitPath).toBe("/api/sessions/s-1/run/steps/draft/steer?item=item-0");
    expect(body).toEqual({ message: "slow down" });
  });

  it("turns a 409 into a StepNotSteerableError carrying the route's own sentence", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, () =>
        HttpResponse.json({ ok: false, message: "this step has already finished" }, { status: 409 }),
      ),
    );

    await expect(stepChatApi.steer("s-1", "draft", "item-0", "slow down")).rejects.toThrow(
      new StepNotSteerableError("this step has already finished"),
    );
  });
});

describe("stepConversationToUiMessages", () => {
  function userPrompt(text: string): MessagePayload {
    return { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] };
  }

  it("keeps thinking as a reasoning part and every tool call, stitched by its own return", () => {
    const messages = stepConversationToUiMessages([
      userPrompt("write the report"),
      {
        kind: "response",
        parts: [
          { part_kind: "thinking", content: "I should read the source first." },
          {
            part_kind: "tool-call",
            tool_name: "read_file",
            tool_call_id: "c-1",
            args: { path: "a.md" },
          },
        ],
      },
      {
        kind: "request",
        parts: [{ part_kind: "tool-return", tool_name: "read_file", tool_call_id: "c-1", content: "the file's text" }],
      },
      { kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] },
    ]);

    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(messages[0]?.parts).toEqual([{ type: "text", text: "write the report" }]);
    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      { type: "reasoning", text: "I should read the source first." },
      {
        type: "tool-read_file",
        toolCallId: "c-1",
        state: "output-available",
        input: { path: "a.md" },
        output: "the file's text",
      },
      { type: "step-start" },
      { type: "text", text: "Here is the report." },
    ]);
  });

  it("marks a tool call with no matching return as pending, and never filters by companion tool", () => {
    const messages = stepConversationToUiMessages([
      userPrompt("go"),
      {
        kind: "response",
        parts: [{ part_kind: "tool-call", tool_name: "run_shell", tool_call_id: "c-9", args: { cmd: "ls" } }],
      },
    ]);

    const assistant = messages[1];
    expect(assistant?.parts).toEqual([
      { type: "step-start" },
      { type: "tool-run_shell", toolCallId: "c-9", state: "output-available", input: { cmd: "ls" }, output: "pending" },
    ]);
  });

  it("pairs a call with a return held only in toolReturnsFrom, absent from the slice itself", () => {
    const wholeTurn: MessagePayload[] = [
      userPrompt("go"),
      {
        kind: "response",
        parts: [{ part_kind: "tool-call", tool_name: "run_shell", tool_call_id: "c-9", args: { cmd: "ls" } }],
      },
      {
        kind: "request",
        parts: [{ part_kind: "tool-return", tool_name: "run_shell", tool_call_id: "c-9", content: "a.md\nb.md" }],
      },
    ];
    // A turn cut into segments for display can end a slice right after the call, its own
    // return carried into the next segment.
    const slice = wholeTurn.slice(0, 2);

    const messages = stepConversationToUiMessages(slice, { toolReturnsFrom: wholeTurn });

    const assistant = messages[1];
    expect(assistant?.parts).toEqual([
      { type: "step-start" },
      { type: "tool-run_shell", toolCallId: "c-9", state: "output-available", input: { cmd: "ls" }, output: "a.md\nb.md" },
    ]);
  });

  it("opens a new user bubble for a later prompt, closing out the assistant bubble before it", () => {
    const messages = stepConversationToUiMessages([
      userPrompt("first"),
      { kind: "response", parts: [{ part_kind: "text", content: "First done." }] },
      userPrompt("second"),
      { kind: "response", parts: [{ part_kind: "text", content: "Second done." }] },
    ]);

    expect(messages.map((m) => [m.role, m.parts])).toEqual([
      ["user", [{ type: "text", text: "first" }]],
      ["assistant", [{ type: "step-start" }, { type: "text", text: "First done." }]],
      ["user", [{ type: "text", text: "second" }]],
      ["assistant", [{ type: "step-start" }, { type: "text", text: "Second done." }]],
    ]);
  });

  it("drops a response row with none of the parts this surface renders", () => {
    const messages = stepConversationToUiMessages([userPrompt("go"), { kind: "response", parts: [] }]);

    expect(messages.map((m) => m.role)).toEqual(["user"]);
  });
});

describe("useStepStream", () => {
  function frame(chunk: Record<string, unknown>): string {
    return `data: ${JSON.stringify(chunk)}\n\n`;
  }

  function streamOf(chunks: Array<Record<string, unknown>>, done = true) {
    const body = chunks.map(frame).join("") + (done ? "data: [DONE]\n\n" : "");
    return new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  it("turns a scripted transcript into the same shape Conversation/AssistantTurn render, closing on [DONE]", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () =>
        streamOf([
          { type: "start", messageId: "m-1" },
          { type: "start-step" },
          { type: "text-start", id: "t-1" },
          { type: "text-delta", id: "t-1", delta: "Look" },
          { type: "text-delta", id: "t-1", delta: "ing." },
          { type: "text-end", id: "t-1" },
          { type: "tool-input-start", toolCallId: "c-1", toolName: "search" },
          { type: "tool-input-available", toolCallId: "c-1", toolName: "search", input: { q: "spec" } },
          { type: "tool-output-available", toolCallId: "c-1", output: "found it" },
          { type: "data-steer", data: { message: "check the appendix too", at: "2026-08-31T10:00:00Z" } },
          { type: "text-start", id: "t-2" },
          { type: "text-delta", id: "t-2", delta: "On it." },
          { type: "text-end", id: "t-2" },
          { type: "finish" },
        ]),
      ),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

    await waitFor(() => expect(result.current.streaming).toBe(false));

    expect(result.current.messages.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(result.current.messages[0]?.parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "Looking.", state: "done" },
      { type: "tool-search", toolCallId: "c-1", state: "output-available", input: { q: "spec" }, output: "found it" },
    ]);
    expect(result.current.messages[1]?.parts).toEqual([{ type: "text", text: "check the appendix too" }]);
    expect(result.current.messages[2]?.parts).toEqual([{ type: "text", text: "On it.", state: "done" }]);
  });

  it("reconnects on a drop at the number of chunks already seen, and stops on [DONE]", async () => {
    let calls = 0;
    const seenSince: string[] = [];
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, ({ request }) => {
        calls += 1;
        seenSince.push(new URL(request.url).searchParams.get("since") ?? "");
        if (calls === 1) {
          return streamOf([{ type: "start" }, { type: "start-step" }], false);
        }
        return streamOf([{ type: "text-start", id: "t-1" }, { type: "text-delta", id: "t-1", delta: "back" }, { type: "finish" }]);
      }),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

    await waitFor(() => expect(result.current.streaming).toBe(false));

    expect(calls).toBe(2);
    expect(seenSince).toEqual(["0", "2"]);
    expect(result.current.messages).toEqual([
      { id: "line-assistant-1", role: "assistant", parts: [{ type: "step-start" }, { type: "text", text: "back", state: "streaming" }] },
    ]);
  });

  it("resumes at the floor's own absolute count after a drop, and flags what the floor dropped", async () => {
    let calls = 0;
    const seenSince: string[] = [];
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, ({ request }) => {
        calls += 1;
        seenSince.push(new URL(request.url).searchParams.get("since") ?? "");
        if (calls === 1) {
          return streamOf(
            [
              { type: "data-replay-floor", data: { floor: 40, requested: 0 } },
              { type: "start" },
              { type: "start-step" },
            ],
            false,
          );
        }
        return streamOf([
          { type: "data-replay-floor", data: { floor: 42, requested: 42 } },
          { type: "text-start", id: "t-1" },
          { type: "text-delta", id: "t-1", delta: "back" },
          { type: "finish" },
        ]);
      }),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

    await waitFor(() => expect(result.current.streaming).toBe(false));

    expect(calls).toBe(2);
    expect(seenSince).toEqual(["0", "42"]);
    // The first connection's own floor (40) already sat past what it asked for (0) -- sticky
    // even once the reconnect's own floor (42) matches exactly what it asked for.
    expect(result.current.droppedBefore).toBe(true);
  });

  it("never flags a drop when the floor matches what was requested", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () =>
        streamOf([{ type: "data-replay-floor", data: { floor: 0, requested: 0 } }, { type: "finish" }]),
      ),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

    await waitFor(() => expect(result.current.streaming).toBe(false));

    expect(result.current.droppedBefore).toBe(false);
  });

  it("skips a malformed frame mid-batch, still folding in the chunks after it", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () => {
        const body =
          frame({ type: "text-start", id: "t-1" }) +
          frame({ type: "text-delta", id: "t-1", delta: "before " }) +
          "data: {not valid json\n\n" +
          frame({ type: "text-delta", id: "t-1", delta: "after" }) +
          frame({ type: "text-end", id: "t-1" }) +
          frame({ type: "finish" }) +
          "data: [DONE]\n\n";
        return new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

    await waitFor(() => expect(result.current.streaming).toBe(false));

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]?.parts).toEqual([{ type: "text", text: "before after", state: "done" }]);
  });

  it("fetches nothing and answers the empty state while disabled", () => {
    let calls = 0;
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () => {
        calls += 1;
        return streamOf([{ type: "finish" }]);
      }),
    );

    const { result } = renderHook(() => useStepStream("s-1", "draft", "item-0", false));

    expect(result.current).toEqual({ messages: [], streaming: false, droppedBefore: false });
    expect(calls).toBe(0);
  });

  describe("reconnect backoff", () => {
    afterEach(() => {
      vi.useRealTimers();
      vi.restoreAllMocks();
    });

    it("waits before a second attempt after a failed fetch, instead of retrying at once", async () => {
      vi.useFakeTimers();
      const fetchSpy = vi
        .spyOn(globalThis, "fetch")
        .mockRejectedValueOnce(new Error("network down"))
        .mockResolvedValueOnce(streamOf([{ type: "finish" }]));

      renderHook(() => useStepStream("s-1", "draft", "item-0", true));

      // The failed attempt has run; the retry must not have fired yet.
      await vi.advanceTimersByTimeAsync(0);
      expect(fetchSpy).toHaveBeenCalledTimes(1);

      // Short of the 500ms floor: still no second attempt.
      await vi.advanceTimersByTimeAsync(300);
      expect(fetchSpy).toHaveBeenCalledTimes(1);

      // Past the floor: the retry fires.
      await vi.advanceTimersByTimeAsync(300);
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });
  });

  describe("unmount", () => {
    afterEach(() => {
      vi.useRealTimers();
      vi.restoreAllMocks();
    });

    it("aborts an open read", async () => {
      vi.useFakeTimers();
      const captured: { signal: AbortSignal | null } = { signal: null };
      vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input: RequestInfo | URL, init?: RequestInit) => {
        captured.signal = init?.signal ?? null;
        return new Promise<Response>(() => {});
      });

      const { unmount } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

      await vi.advanceTimersByTimeAsync(0);
      expect(captured.signal?.aborted).toBe(false);

      unmount();

      expect(captured.signal?.aborted).toBe(true);
    });

    it("stops a pending reconnect backoff, no second fetch ever fires", async () => {
      vi.useFakeTimers();
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("network down"));

      const { unmount } = renderHook(() => useStepStream("s-1", "draft", "item-0", true));

      await vi.advanceTimersByTimeAsync(0);
      expect(fetchSpy).toHaveBeenCalledTimes(1);

      unmount();

      await vi.advanceTimersByTimeAsync(5000);
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
  });
});
