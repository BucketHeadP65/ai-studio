import { describe, expect, it } from "vitest";

import { groupWorkActs } from "./work-groups";

import type { ToolUIPart } from "ai";

function part(toolCallId: string, type: `tool-${string}`, input: unknown): ToolUIPart {
  return { type, toolCallId, state: "output-available", input, output: "" };
}

describe("groupWorkActs", () => {
  it("returns an empty list for no parts", () => {
    expect(groupWorkActs([])).toEqual([]);
  });

  it("merges consecutive same-kind parts into one group", () => {
    const parts = [
      part("c-1", "tool-run_command", { command: "ls" }),
      part("c-2", "tool-run_command", { command: "pwd" }),
      part("c-3", "tool-run_command", { command: "echo hi" }),
    ];

    const groups = groupWorkActs(parts);

    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("execute");
    expect(groups[0].parts).toEqual(parts);
  });

  it("splits a run when a different kind interrupts it", () => {
    const parts = [
      part("c-1", "tool-run_command", { command: "ls" }),
      part("c-2", "tool-read_file", { path: "docs/plan.md" }),
      part("c-3", "tool-run_command", { command: "pwd" }),
    ];

    const groups = groupWorkActs(parts);

    expect(groups.map((group) => group.kind)).toEqual(["execute", "read", "execute"]);
    expect(groups.map((group) => group.parts.length)).toEqual([1, 1, 1]);
    const allParts = groups.flatMap((group) => group.parts);
    expect(allParts).toEqual(parts);
  });

  it("keeps a single part as its own group", () => {
    const parts = [part("c-1", "tool-read_file", { path: "docs/plan.md" })];

    expect(groupWorkActs(parts)).toEqual([{ kind: "read", parts }]);
  });

  it("folds a listing and a search together, both kind search", () => {
    const parts = [
      part("c-1", "tool-list_directory", { path: "src" }),
      part("c-2", "tool-search_files", { pattern: "plan" }),
    ];

    const groups = groupWorkActs(parts);

    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("search");
    expect(groups[0].parts).toEqual(parts);
  });
});
