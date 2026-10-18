import { getToolName } from "ai";

import { presentTool } from "./tool-presentation";

import type { ToolKind } from "./tool-presentation";
import type { ActPart } from "./turn-fold";

// Consecutive work acts of the same kind, read off a step in call order. A run of same-kind
// calls (three shell commands in a row) folds into one group the bubble can show as a single
// line; a lone call, or a kind change, keeps its own group.

export interface WorkGroup {
  kind: ToolKind;
  parts: ActPart[];
}

export function groupWorkActs(parts: ActPart[]): WorkGroup[] {
  const groups: WorkGroup[] = [];
  for (const part of parts) {
    const kind = presentTool(getToolName(part), part.input).kind;
    const last = groups.at(-1);
    if (last !== undefined && last.kind === kind) {
      last.parts.push(part);
    } else {
      groups.push({ kind, parts: [part] });
    }
  }
  return groups;
}
