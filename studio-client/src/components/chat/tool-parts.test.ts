import { describe, expect, it } from "vitest";

import {
  hasActOutcome,
  lastStepToolParts,
  pendingApprovals,
  toolResultsOf,
  unresolvedVerbs,
} from "./tool-parts";

import type { UIMessage } from "ai";

const user: UIMessage = { id: "u1", role: "user", parts: [{ type: "text", text: "go" }] };

function assistant(parts: UIMessage["parts"]): UIMessage {
  return { id: "a1", role: "assistant", parts };
}

const navigate = {
  type: "tool-navigate" as const,
  toolCallId: "c-1",
  state: "input-available" as const,
  input: { url: "/chat" },
};
const toggle = {
  type: "tool-toggle_plugin" as const,
  toolCallId: "c-2",
  state: "approval-requested" as const,
  input: { name: "sql", on: true },
  approval: { id: "c-2" },
};

describe("lastStepToolParts", () => {
  it("reads only the parts after the last step-start", () => {
    const message = assistant([{ type: "step-start" }, navigate, { type: "step-start" }, toggle]);
    expect(lastStepToolParts(message).map((part) => part.toolCallId)).toEqual(["c-2"]);
  });
  it("reads every tool part when no step-start exists (a bubble built by hand)", () => {
    expect(lastStepToolParts(assistant([navigate, toggle]))).toHaveLength(2);
  });
});

describe("pendingApprovals and unresolvedVerbs", () => {
  it("names the approval requests and the verbs still without an output", () => {
    const messages = [user, assistant([{ type: "step-start" }, navigate, toggle])];
    expect(pendingApprovals(messages)).toEqual([
      {
        approvalId: "c-2",
        toolCallId: "c-2",
        toolName: "toggle_plugin",
        input: { name: "sql", on: true },
      },
    ]);
    expect(unresolvedVerbs(messages).map((part) => part.toolCallId)).toEqual(["c-1"]);
  });
  it("is empty when the last message is the person's", () => {
    expect(pendingApprovals([assistant([toggle]), user])).toEqual([]);
    expect(unresolvedVerbs([assistant([navigate]), user])).toEqual([]);
  });
});

describe("toolResultsOf", () => {
  it("carries verb outputs and approval answers from the last assistant message", () => {
    const messages = [
      user,
      assistant([
        { type: "step-start" },
        { ...navigate, state: "output-available" as const, output: "navigated to /chat" },
        {
          ...toggle,
          state: "approval-responded" as const,
          approval: { id: "c-2", approved: false, reason: "superseded" },
        },
        {
          type: "tool-search" as const,
          toolCallId: "c-3",
          state: "output-available" as const,
          input: {},
          output: "hits",
        },
      ]),
    ];
    expect(toolResultsOf(messages, new Set(["c-1", "c-2"]))).toEqual([
      { tool_call_id: "c-1", tool_name: "navigate", output: "navigated to /chat" },
      { tool_call_id: "c-2", tool_name: "toggle_plugin", approved: false, reason: "superseded" },
    ]);
  });

  it("carries a verb outcome only for a call this browser answered", () => {
    // What a read-back transcript looks like: a call the browser never answered comes back as an
    // output-available part carrying the transcript's own pending marker, no step boundary before
    // it. Reporting that would tell the model the verb returned the word pending.
    const messages = [
      user,
      assistant([
        { ...navigate, state: "output-available" as const, output: "pending" },
        {
          ...navigate,
          toolCallId: "c-7",
          state: "output-available" as const,
          output: "navigated to /chat",
        },
      ]),
    ];

    expect(toolResultsOf(messages, new Set(["c-7"]))).toEqual([
      { tool_call_id: "c-7", tool_name: "navigate", output: "navigated to /chat" },
    ]);
    expect(toolResultsOf(messages, new Set())).toEqual([]);
  });

  it("still reads the previous assistant message when the person just typed (a supersede)", () => {
    const messages = [
      assistant([
        {
          ...toggle,
          state: "approval-responded" as const,
          approval: { id: "c-2", approved: false, reason: "superseded" },
        },
      ]),
      user,
    ];
    expect(toolResultsOf(messages, new Set(["c-2"]))).toHaveLength(1);
  });
});

describe("hasActOutcome", () => {
  const pluginOn = {
    ...toggle,
    state: "output-available" as const,
    output: "Plugin sql is on.",
    approval: { id: "c-2", approved: true as const },
  };

  it("is true once a console act has an output", () => {
    expect(hasActOutcome(assistant([pluginOn]))).toBe(true);
    expect(hasActOutcome(assistant([navigate]))).toBe(false);
  });

  it("is true when the act ran a step before the closing text", () => {
    const message = assistant([
      { type: "step-start" },
      pluginOn,
      { type: "step-start" },
      { type: "text", text: "The sql plugin is on now." },
    ]);
    expect(hasActOutcome(message)).toBe(true);
  });

  it("is false when the same two-step turn only ever called a verb", () => {
    const message = assistant([
      { type: "step-start" },
      { ...navigate, state: "output-available" as const, output: "navigated to /chat" },
      { type: "step-start" },
      { type: "text", text: "You are on the chat page now." },
    ]);
    expect(hasActOutcome(message)).toBe(false);
  });
});
