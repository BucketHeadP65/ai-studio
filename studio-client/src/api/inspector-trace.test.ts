import { describe, expect, it } from "vitest";

import type { MessagePayload } from "./sessions-types";
import { traceCounts } from "./inspector-trace";

// One conversation, built the same way the harness records it: a request row opening on the
// view the inspector read, a response with thinking plus two reading calls, their returns, a
// refused answer, a retry-feedback request, the accepted answer, and two follow-ups.
const MESSAGES: MessagePayload[] = [
  { kind: "request", parts: [{ part_kind: "user-prompt", content: "Symptom: warning_ignored." }] },
  {
    kind: "response",
    parts: [
      { part_kind: "thinking", content: "" },
      { part_kind: "tool-call", tool_name: "outline", tool_call_id: "t1", args: {} },
      { part_kind: "tool-call", tool_name: "read", tool_call_id: "t2", args: { step: 0 } },
      { part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "t3", args: { symptom: "not_in_taxonomy" } },
    ],
  },
  {
    kind: "request",
    parts: [
      { part_kind: "tool-return", tool_name: "outline", tool_call_id: "t1", content: "step 0", outcome: "success" },
      { part_kind: "tool-return", tool_name: "read", tool_call_id: "t2", content: "a warning", outcome: "success" },
      { part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "t3", content: "Unknown symptom", outcome: "retried" },
    ],
  },
  { kind: "request", parts: [{ part_kind: "retry-feedback", content: "field required", retry: { cause: "validation_error" } }] },
  {
    kind: "response",
    parts: [{ part_kind: "tool-call", tool_name: "final_result_Attribution", tool_call_id: "t4", args: { symptom: "warning_ignored" } }],
  },
  {
    kind: "request",
    parts: [
      { part_kind: "tool-return", tool_name: "final_result_Attribution", tool_call_id: "t4", content: "Final result processed." },
      { part_kind: "user-prompt", content: "why not the instructions?" },
    ],
  },
  { kind: "response", parts: [{ part_kind: "text", content: "Because nothing covered it." }] },
  { kind: "request", parts: [{ part_kind: "user-prompt", content: "and the tool?" }] },
];

describe("traceCounts", () => {
  it("counts every call but the answer as a read, each refusal, and every prompt after the first", () => {
    expect(traceCounts(MESSAGES)).toEqual({ reads: 2, refusals: 2, followUps: 2 });
  });

  it("counts nothing in an empty conversation", () => {
    expect(traceCounts([])).toEqual({ reads: 0, refusals: 0, followUps: 0 });
  });
});
