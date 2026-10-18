import type { UIMessage } from "ai";
import { describe, expect, it } from "vitest";

import { transcriptToUiMessages } from "@/api/sessions";
import type { TranscriptMessage } from "@/api/sessions-types";

import { foldTurn } from "./turn-fold";

function assistant(parts: UIMessage["parts"], id = "a1"): UIMessage {
  return { id, role: "assistant", parts };
}

const NAVIGATE: UIMessage["parts"][number] = {
  type: "tool-navigate",
  toolCallId: "c-1",
  state: "output-available",
  input: { url: "/chat" },
  output: "navigated to /chat",
};

describe("foldTurn", () => {
  it("reads the last step as the answer and every step before it as the trace", () => {
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        NAVIGATE,
        { type: "step-start" },
        { type: "text", text: "Checking the tests." },
        { type: "step-start" },
        { type: "text", text: "All three read in full." },
      ]),
    );

    expect(folded.last?.text).toBe("All three read in full.");
    expect(folded.last?.acts).toEqual([]);
    expect(folded.earlier.map((step) => step.text)).toEqual([
      "Reading the plan.",
      "Checking the tests.",
    ]);
    expect(folded.earlier[0]?.acts).toEqual([NAVIGATE]);
  });

  it("treats a message with no step boundary as one step with nothing to fold", () => {
    const folded = foldTurn(assistant([{ type: "text", text: "Drafted lit-check." }]));

    expect(folded.earlier).toEqual([]);
    expect(folded.last?.text).toBe("Drafted lit-check.");
  });

  it("keeps the previous step as the answer while a freshly opened step is still empty", () => {
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "text", text: "Reading the remaining halves." },
        { type: "step-start" },
      ]),
    );

    expect(folded.earlier).toEqual([]);
    expect(folded.last?.text).toBe("Reading the remaining halves.");
  });

  it("does not count a step that only said whitespace before acting through a tool the bubble never shows", () => {
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "text", text: "\n" },
        { type: "step-start" },
        { type: "text", text: "  Reading the plan.\n" },
        { type: "step-start" },
        { type: "text", text: "Done." },
      ]),
    );

    expect(folded.earlier.map((step) => step.text)).toEqual(["Reading the plan."]);
    expect(folded.last?.text).toBe("Done.");
  });

  it("counts a step whose only act is a work tool and carries it in workActs", () => {
    const readFile: UIMessage["parts"][number] = {
      type: "tool-read_file",
      toolCallId: "c-9",
      state: "input-available",
      input: { path: "docs/plan.md" },
    };
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "text", text: "Reading the plan." },
        { type: "step-start" },
        readFile,
      ]),
    );

    expect(folded.earlier.map((step) => step.text)).toEqual(["Reading the plan."]);
    expect(folded.last?.text).toBe("");
    expect(folded.last?.acts).toEqual([]);
    expect(folded.last?.workActs).toEqual([readFile]);
  });

  it("keeps companion acts in acts and out of workActs", () => {
    const folded = foldTurn(assistant([{ type: "step-start" }, NAVIGATE]));

    expect(folded.last?.acts).toEqual([NAVIGATE]);
    expect(folded.last?.workActs).toEqual([]);
  });

  it("leaves the run tool out of workActs, its run card already stands in for it", () => {
    const runCall: UIMessage["parts"][number] = {
      type: "tool-run_workflow",
      toolCallId: "c-4",
      state: "output-available",
      input: { workflow_id: "wf-1" },
      output: "started",
    };
    const folded = foldTurn(assistant([{ type: "step-start" }, runCall]));

    expect(folded.last).toBeNull();
  });

  it("joins the text parts of one step and ignores reasoning", () => {
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "reasoning", text: "hmm" },
        { type: "text", text: "Reading " },
        { type: "text", text: "the plan." },
      ]),
    );

    expect(folded.earlier).toEqual([]);
    expect(folded.last?.text).toBe("Reading the plan.");
  });

  it("gives every step a key of its own", () => {
    const folded = foldTurn(
      assistant([
        { type: "step-start" },
        { type: "text", text: "one" },
        { type: "step-start" },
        { type: "text", text: "two" },
      ]),
    );

    expect(folded.earlier[0]?.key).not.toBe(folded.last?.key);
  });

  it("folds a stored turn exactly as its live twin", () => {
    const transcript: TranscriptMessage[] = [
      {
        run_id: "r1",
        idx: 0,
        kind: "request",
        payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "go" }] },
        text: "go",
      },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [
            { part_kind: "text", content: "Reading the plan." },
            {
              part_kind: "tool-call",
              tool_name: "navigate",
              args: { url: "/chat" },
              tool_call_id: "c-1",
            },
          ],
        },
        text: "Reading the plan.",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "request",
        payload: {
          kind: "request",
          parts: [
            {
              part_kind: "tool-return",
              tool_name: "navigate",
              content: "navigated to /chat",
              tool_call_id: "c-1",
            },
          ],
        },
        text: "",
      },
      {
        run_id: "r1",
        idx: 3,
        kind: "response",
        payload: {
          kind: "response",
          parts: [{ part_kind: "text", content: "All three read in full." }],
        },
        text: "All three read in full.",
      },
    ];
    const live = assistant([
      { type: "step-start" },
      { type: "text", text: "Reading the plan." },
      NAVIGATE,
      { type: "step-start" },
      { type: "text", text: "All three read in full." },
    ]);

    const stored = transcriptToUiMessages(transcript).find(
      (message) => message.role === "assistant",
    );
    expect(stored).toBeDefined();
    if (stored === undefined) return;

    const strip = (folded: ReturnType<typeof foldTurn>) => ({
      earlier: folded.earlier.map((step) => ({
        text: step.text,
        acts: step.acts,
        workActs: step.workActs,
      })),
      last:
        folded.last === null
          ? null
          : { text: folded.last.text, acts: folded.last.acts, workActs: folded.last.workActs },
    });
    expect(strip(foldTurn(stored))).toEqual(strip(foldTurn(live)));
  });
});
