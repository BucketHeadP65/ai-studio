import { describe, expect, it } from "vitest";

import type { MessagePayload } from "./sessions-types";
import { traceCounts, traceEntries } from "./inspector-trace";

// One conversation, built the same way the harness records it: a request row opening on the
// view the inspector read, a response with thinking plus a tool call, the tool's return in the
// next request, a retry-feedback request, and a response settling on `final_result_Attribution`.
const MESSAGES: MessagePayload[] = [
  {
    kind: "request",
    parts: [{ part_kind: "user-prompt", content: "Symptom: warning_ignored." }],
  },
  {
    kind: "response",
    parts: [
      { part_kind: "thinking", content: "Let me read the step." },
      { part_kind: "tool-call", tool_name: "read", tool_call_id: "t1", args: { step: 0, start: 0, end: 8 } },
    ],
  },
  {
    kind: "request",
    parts: [{ part_kind: "tool-return", tool_name: "read", tool_call_id: "t1", content: { seen: "a warning" } }],
  },
  {
    kind: "request",
    parts: [{ part_kind: "retry-feedback", content: "field required", retry: { cause: "validation_error" } }],
  },
  {
    kind: "response",
    parts: [
      {
        part_kind: "tool-call",
        tool_name: "final_result_Attribution",
        tool_call_id: "t2",
        args: { root_cause: "tool_design", surface_kind: "tool", surface_name: "write_note" },
      },
    ],
  },
  {
    kind: "request",
    parts: [{ part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "t2", content: "Final result processed." }],
  },
];

describe("traceEntries", () => {
  it("does not accept an output tool the framework skipped after another result won", () => {
    const entries = traceEntries([
      { kind: "response", parts: [
        { part_kind: "tool-call", tool_name: "final_result_NoIssue", tool_call_id: "selected", args: { reason: "Justified." } },
        { part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "skipped", args: { invalid: "not even schema valid" } },
      ] },
      { kind: "request", parts: [
        { part_kind: "tool-return", tool_name: "final_result_NoIssue", tool_call_id: "selected", content: "Final result processed.", outcome: "success" },
        { part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "skipped", content: "Output tool not used - a final result was already processed.", outcome: "success" },
      ] },
    ]);

    expect(entries).toMatchObject([{ kind: "answered", outcome: "dismissed" }, { kind: "answered", outcome: "unvalidated" }]);
  });

  it("never treats the final exhausted output as accepted when no output return was recorded", () => {
    const entries = traceEntries([
      { kind: "response", parts: [{ part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "bad-1", args: { symptom: "not_in_taxonomy" } }] },
      { kind: "request", parts: [{ part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "bad-1", content: "Unknown symptom", outcome: "retried" }] },
      { kind: "response", parts: [{ part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "bad-2", args: { symptom: "not_in_taxonomy" } }] },
    ]);

    expect(entries).toMatchObject([
      { kind: "refused", rejected: { symptom: "not_in_taxonomy" } },
      { kind: "answered", outcome: "unvalidated", args: { symptom: "not_in_taxonomy" } },
    ]);
    expect(entries.some((entry) => entry.kind === "answered" && entry.outcome === "finding")).toBe(false);
  });

  it("reads a conversation as told, thinking, a call with its return, a refusal and the answer", () => {
    const entries = traceEntries(MESSAGES);

    expect(entries.map((entry) => entry.kind)).toEqual(["told", "thinking", "call", "refused", "answered"]);
    expect(entries[0]).toEqual({ kind: "told", text: "Symptom: warning_ignored." });
    expect(entries[2]).toEqual({ kind: "call", name: "read", line: "read(step: 0, start: 0, end: 8)", returned: "seen: a warning" });
    expect(entries[3]).toEqual({ kind: "refused", text: "field required", rejected: null });
    expect(entries[4]).toMatchObject({ kind: "answered", args: { root_cause: "tool_design" } });
  });

  it("names the rejected call's own args on the refusal, and drops its answered entry", () => {
    const entries = traceEntries([
      {
        kind: "response",
        parts: [
          { part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "t-first", args: { root_cause: "tool_design" } },
        ],
      },
      {
        kind: "request",
        parts: [{ part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "t-first", content: "field required", outcome: "retried" }],
      },
      {
        kind: "response",
        parts: [
          { part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "t-second", args: { root_cause: "agent_reasoning" } },
        ],
      },
    ]);

    expect(entries.map((entry) => entry.kind)).toEqual(["refused", "answered"]);
    expect(entries.filter((entry) => entry.kind === "answered")).toHaveLength(1);
    expect(entries[0]).toMatchObject({ kind: "refused", rejected: { root_cause: "tool_design" } });
  });

  it("reads a later prompt as asked and the prose after it as said", () => {
    const entries = traceEntries([
      { kind: "request", parts: [{ part_kind: "user-prompt", content: "Symptom: warning_ignored." }] },
      { kind: "response", parts: [{ part_kind: "text", content: "Looking." }] },
      { kind: "request", parts: [{ part_kind: "user-prompt", content: "why not the instructions?" }] },
      { kind: "response", parts: [{ part_kind: "text", content: "Because nothing covered it." }] },
    ]);

    expect(entries).toEqual([
      { kind: "told", text: "Symptom: warning_ignored." },
      { kind: "said", text: "Looking." },
      { kind: "asked", text: "why not the instructions?" },
      { kind: "said", text: "Because nothing covered it." },
    ]);
  });

  it("reads a retried return of an ordinary tool as a refusal, never as the call's return", () => {
    const entries = traceEntries([
      { kind: "response", parts: [{ part_kind: "tool-call", tool_name: "read", tool_call_id: "t3", args: { step: 9 } }] },
      { kind: "request", parts: [{ part_kind: "tool-return", tool_name: "read", tool_call_id: "t3", content: "no step 9", outcome: "retried" }] },
    ]);

    expect(entries).toEqual([
      { kind: "call", name: "read", line: "read(step: 9)", returned: null },
      { kind: "refused", text: "no step 9", rejected: null },
    ]);
  });

  it("marks a call whose return never came", () => {
    const entries = traceEntries([{ kind: "response", parts: [{ part_kind: "tool-call", tool_name: "read", tool_call_id: "t9", args: {} }] }]);

    expect(entries).toEqual([{ kind: "call", name: "read", line: "read()", returned: null }]);
  });

  it("joins a refusal's error list with commas", () => {
    const entries = traceEntries([
      { kind: "request", parts: [{ part_kind: "retry-feedback", content: [{ msg: "field required" }, { msg: "too short" }], retry: { cause: "validation_error" } }] },
    ]);

    expect(entries).toEqual([{ kind: "refused", text: "field required, too short", rejected: null }]);
  });

  it("counts the reads, the refusals and the follow-ups", () => {
    expect(traceCounts(traceEntries(MESSAGES))).toEqual({ reads: 1, refusals: 1, followUps: 0 });
  });
});
