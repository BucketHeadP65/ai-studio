import type { UIMessage } from "ai";
import { describe, expect, it } from "vitest";

import type { TranscriptMessage } from "@/api/sessions-types";

import { runToolCallEntries } from "./run-tool-calls";

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] };
}

function promptEntry(runId: string, idx: number, text: string): TranscriptMessage {
  return {
    run_id: runId,
    idx,
    kind: "request",
    payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] },
    text,
  };
}

function toolCallEntry(
  runId: string,
  idx: number,
  toolName: string,
  args: unknown,
): TranscriptMessage {
  return {
    run_id: runId,
    idx,
    kind: "response",
    payload: { kind: "response", parts: [{ part_kind: "tool-call", tool_name: toolName, args }] },
    text: "",
  };
}

describe("runToolCallEntries", () => {
  it("attaches after the run's own last rendered bubble", () => {
    const messages = [
      textMessage("r1:0", "user", "start it"),
      textMessage("r1:2", "assistant", "on it"),
    ];
    const transcript = [toolCallEntry("r1", 1, "run_workflow", { workflow_id: "wf.lit-check.v5" })];

    expect(runToolCallEntries(messages, transcript)).toEqual([
      { afterMessageId: "r1:2", workflowId: "wf.lit-check.v5" },
    ]);
  });

  it("reads args given as a JSON string, the same shape a stored transcript row carries", () => {
    const messages = [textMessage("r1:0", "assistant", "on it")];
    const transcript = [
      toolCallEntry("r1", 0, "run_workflow", JSON.stringify({ workflow_id: "wf.demo" })),
    ];

    expect(runToolCallEntries(messages, transcript)).toEqual([
      { afterMessageId: "r1:0", workflowId: "wf.demo" },
    ]);
  });

  it("ignores a tool call to any other tool", () => {
    const messages = [textMessage("r1:0", "assistant", "on it")];
    const transcript = [toolCallEntry("r1", 0, "check_draft", { anything: true })];

    expect(runToolCallEntries(messages, transcript)).toEqual([]);
  });

  it("drops an entry whose run never produced a rendered bubble", () => {
    const messages: UIMessage[] = [];
    const transcript = [toolCallEntry("r1", 0, "run_workflow", { workflow_id: "wf.demo" })];

    expect(runToolCallEntries(messages, transcript)).toEqual([]);
  });

  it("keeps each turn's own entries separate", () => {
    const messages = [
      textMessage("r1:0", "user", "one"),
      textMessage("r1:1", "assistant", "first"),
      textMessage("r2:0", "user", "two"),
      textMessage("r2:1", "assistant", "second"),
    ];
    const transcript = [
      promptEntry("r1", 0, "one"),
      toolCallEntry("r1", 1, "run_workflow", { workflow_id: "wf.one" }),
      promptEntry("r2", 0, "two"),
      toolCallEntry("r2", 1, "run_workflow", { workflow_id: "wf.two" }),
    ];

    expect(runToolCallEntries(messages, transcript)).toEqual([
      { afterMessageId: "r1:1", workflowId: "wf.one" },
      { afterMessageId: "r2:1", workflowId: "wf.two" },
    ]);
  });

  it("lands after the turn's last bubble even when the run tool ran before the turn resumed on a new run", () => {
    const messages = [
      textMessage("r1:0", "user", "go"),
      textMessage("r2:1", "assistant", "started"),
    ];
    const transcript = [
      promptEntry("r1", 0, "go"),
      toolCallEntry("r1", 1, "run_workflow", { workflow_id: "wf.one" }),
      {
        run_id: "r2",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [{ part_kind: "tool-return", tool_name: "set_tab", content: "ok" }],
        },
        text: "",
      },
    ];

    expect(runToolCallEntries(messages, transcript)).toEqual([
      { afterMessageId: "r2:1", workflowId: "wf.one" },
    ]);
  });

  it("renders no entries when nothing called the run tool", () => {
    const messages = [textMessage("r1:0", "assistant", "just talking")];
    expect(runToolCallEntries(messages, [])).toEqual([]);
  });
});
