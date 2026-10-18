import { afterEach, describe, expect, it, vi } from "vitest";

import type { MockInstance } from "vitest";

import { ChatStreamError, chatStream } from "./chat-stream";

// The request is pydantic-ai's own submit-message body, posted to the named session's
// messages path. The reply is the Vercel AI data-stream protocol (v6): one JSON chunk
// per `data:` line, closed by `data: [DONE]`. The parser maps text-delta chunks
// to live text, tool-input-start chunks to named acts, the one data-settle chunk
// to the settled ChatReply, and an error chunk to a thrown sentence.

const SETTLE = {
  message: "Here is a draft.",
  draft: { spec_yaml: "", title: "", summary: "", applicability: "" },
  issues: [],
  valid: false,
  graph: null,
};

function sse(chunks: (object | "[DONE]")[]): Response {
  const body = chunks
    .map((chunk) => `data: ${chunk === "[DONE]" ? "[DONE]" : JSON.stringify(chunk)}\n\n`)
    .join("");
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

// What the one recorded call put on the wire, decoded from the request init the client built.
function sentBody(spy: MockInstance<typeof fetch>): unknown {
  const init = spy.mock.calls[0]?.[1];
  const body = init?.body;
  return typeof body === "string" ? JSON.parse(body) : null;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("chatStream", () => {
  it("posts the submit-message body to the named session and settles on the data-settle chunk", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([
        { type: "start" },
        { type: "text-start", id: "t1" },
        { type: "text-delta", id: "t1", delta: "Here is " },
        { type: "text-delta", id: "t1", delta: "a draft." },
        { type: "tool-input-start", toolCallId: "c1", toolName: "search" },
        { type: "data-settle", data: SETTLE },
        // The live frame order on a one-step turn: the step reading lands after the settle, so
        // this client may not stop reading at the settle. It passes the chunk over and keeps going.
        { type: "data-step", data: { tool_call_ids: [], usage: {}, timestamp: "" } },
        { type: "finish" },
        "[DONE]",
      ]),
    );
    const deltas: string[] = [];
    const acts: string[] = [];

    const reply = await chatStream("s-1", "hello", {
      onDelta: (text) => deltas.push(text),
      onStatus: (note) => acts.push(note),
    });

    expect(fetchSpy).toHaveBeenCalledWith(
      "http://studio.test/api/sessions/s-1/messages",
      expect.objectContaining({ method: "POST" }),
    );
    // Whole, so a stray field would fail here. The chat id is the session; the message's own id
    // is minted fresh per message, so it is asserted by shape, the only value a test cannot spell.
    expect(sentBody(fetchSpy)).toEqual({
      trigger: "submit-message",
      id: "s-1",
      messages: [
        {
          id: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/),
          role: "user",
          parts: [{ type: "text", text: "hello" }],
        },
      ],
    });
    expect(deltas.join("")).toBe("Here is a draft.");
    expect(acts).toEqual(["running search"]);
    expect(reply.message).toBe("Here is a draft.");
  });

  it("throws the error chunk's sentence", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([
        { type: "start" },
        { type: "error", errorText: "the turn ran out of budget" },
        "[DONE]",
      ]),
    );

    await expect(chatStream("s-1", "hello", { onDelta: () => {}, onStatus: () => {} })).rejects.toThrow(
      "the turn ran out of budget",
    );
  });

  it("refuses a stream that closes without settling", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([{ type: "start" }, { type: "finish" }, "[DONE]"]),
    );

    await expect(chatStream("s-1", "hello", { onDelta: () => {}, onStatus: () => {} })).rejects.toThrow(
      ChatStreamError,
    );
  });

  it("surfaces a non-stream refusal's detail sentence", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "a turn is already streaming; wait for it to finish" }),
        { status: 409 },
      ),
    );

    await expect(chatStream("s-1", "hello", { onDelta: () => {}, onStatus: () => {} })).rejects.toThrow(
      "a turn is already streaming",
    );
  });
});
