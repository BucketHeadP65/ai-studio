import { describe, expect, it, vi } from "vitest";

import { StepUsageSchema, stepUsages, toolFacts, turnTokensOut } from "./step-usage";

import type { UIMessage } from "ai";

// The chat stream's own `data-step` part: one per model response, carrying the tool calls it
// made, what it spent, and when. The transcript converter builds the same shape from stored
// rows, so a reloaded turn and a live one read identically.

function assistant(parts: UIMessage["parts"]): UIMessage {
  return { id: "a1", role: "assistant", parts };
}

function usage(outputTokens: number) {
  return {
    input_tokens: 100,
    output_tokens: outputTokens,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  };
}

describe("StepUsageSchema", () => {
  it("accepts the wire shape with no returns", () => {
    const parsed = StepUsageSchema.safeParse({
      tool_call_ids: ["call_abc"],
      usage: usage(51),
      timestamp: "2026-08-28T23:04:22.206741Z",
    });
    expect(parsed.success).toBe(true);
  });

  it("accepts returns keyed by tool call id", () => {
    const parsed = StepUsageSchema.safeParse({
      tool_call_ids: ["call_abc"],
      usage: usage(51),
      timestamp: "2026-08-28T23:04:22.206741Z",
      returns: { call_abc: "2026-08-28T23:04:23.000000Z" },
    });
    expect(parsed.success).toBe(true);
  });

  it("rejects a payload missing usage", () => {
    const parsed = StepUsageSchema.safeParse({
      tool_call_ids: [],
      timestamp: "2026-08-28T23:04:22.206741Z",
    });
    expect(parsed.success).toBe(false);
  });
});

describe("stepUsages", () => {
  it("reads data-step parts off the message in order", () => {
    const message = assistant([
      { type: "step-start" },
      {
        type: "data-step",
        data: { tool_call_ids: ["c-1"], usage: usage(10), timestamp: "2026-08-28T23:00:00Z" },
      },
      { type: "step-start" },
      {
        type: "data-step",
        data: { tool_call_ids: ["c-2"], usage: usage(20), timestamp: "2026-08-28T23:00:05Z" },
      },
    ]);

    const steps = stepUsages(message);

    expect(steps).toHaveLength(2);
    expect(steps[0]?.usage.output_tokens).toBe(10);
    expect(steps[1]?.usage.output_tokens).toBe(20);
  });

  it("skips a data-step part that fails the contract, and logs it", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const message = assistant([
      { type: "step-start" },
      { type: "data-step", data: { tool_call_ids: ["c-1"] } },
    ]);

    expect(stepUsages(message)).toEqual([]);
    expect(errorSpy).toHaveBeenCalledOnce();
    errorSpy.mockRestore();
  });

  it("reads no steps off a message with no data-step parts", () => {
    const message = assistant([{ type: "step-start" }, { type: "text", text: "hi" }]);
    expect(stepUsages(message)).toEqual([]);
  });
});

describe("turnTokensOut", () => {
  it("sums output tokens across every step", () => {
    const message = assistant([
      {
        type: "data-step",
        data: { tool_call_ids: [], usage: usage(10), timestamp: "2026-08-28T23:00:00Z" },
      },
      {
        type: "data-step",
        data: { tool_call_ids: [], usage: usage(20), timestamp: "2026-08-28T23:00:05Z" },
      },
    ]);

    expect(turnTokensOut(message)).toBe(30);
  });

  it("is null when the message has no step parts", () => {
    const message = assistant([{ type: "text", text: "hi" }]);
    expect(turnTokensOut(message)).toBeNull();
  });
});

describe("toolFacts", () => {
  it("keys facts by tool call id, from the step that made the call", () => {
    const message = assistant([
      {
        type: "data-step",
        data: {
          tool_call_ids: ["c-1", "c-2"],
          usage: usage(51),
          timestamp: "2026-08-28T23:00:00Z",
          returns: { "c-1": "2026-08-28T23:00:01Z" },
        },
      },
    ]);

    const facts = toolFacts(message);

    expect(facts.get("c-1")).toEqual({
      tokensOut: 51,
      startedAt: "2026-08-28T23:00:00Z",
      returnedAt: "2026-08-28T23:00:01Z",
    });
    expect(facts.get("c-2")).toEqual({
      tokensOut: 51,
      startedAt: "2026-08-28T23:00:00Z",
      returnedAt: null,
    });
  });

  it("is empty for a message with no data-step parts", () => {
    const message = assistant([{ type: "text", text: "hi" }]);
    expect(toolFacts(message).size).toBe(0);
  });
});
