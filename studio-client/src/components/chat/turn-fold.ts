import { getToolName, isDynamicToolUIPart, isTextUIPart, isToolUIPart } from "ai";

import { isCompanionTool } from "@/api/page-context";

import { RUN_TOOL_NAME } from "./run-tool-calls";

import type { DynamicToolUIPart, ToolUIPart, UIMessage } from "ai";

// One assistant turn, read as the steps the model took. A step is everything between two
// `step-start` parts: the text the model said before acting (its narration), the companion
// acts it made, and its work tool calls — every other tool the agent used, carried separately
// so the bubble can draw them as cards. The run tool alone stays out: its run card already
// stands in for it. The last step is the turn's answer, every step before it is the trace the
// bubble folds away. A message with no `step-start` at all (one built by hand, or a turn that
// never acted) is a single step. A step with nothing to show — the boundary a stream has
// opened but not filled — does not count, so the previous step stays the answer until the new
// one says something.

export type ActPart = ToolUIPart | DynamicToolUIPart;

export interface TurnStep {
  key: string;
  text: string;
  acts: ActPart[];
  workActs: ActPart[];
}

export interface FoldedTurn {
  earlier: TurnStep[];
  last: TurnStep | null;
}

function isActPart(part: UIMessage["parts"][number]): part is ActPart {
  return isToolUIPart(part) || isDynamicToolUIPart(part);
}

function isEmpty(step: TurnStep): boolean {
  return step.text === "" && step.acts.length === 0 && step.workActs.length === 0;
}

export function foldTurn(message: UIMessage): FoldedTurn {
  const steps: TurnStep[] = [];
  let current: TurnStep = { key: `${message.id}:0`, text: "", acts: [], workActs: [] };
  const close = (): void => {
    const step = { ...current, text: current.text.trim() };
    if (!isEmpty(step)) steps.push(step);
    current = { key: `${message.id}:${steps.length}`, text: "", acts: [], workActs: [] };
  };
  for (const part of message.parts) {
    if (part.type === "step-start") {
      close();
    } else if (isTextUIPart(part)) {
      current = { ...current, text: current.text + part.text };
    } else if (isActPart(part)) {
      const name = getToolName(part);
      if (isCompanionTool(name)) {
        current = { ...current, acts: [...current.acts, part] };
      } else if (name !== RUN_TOOL_NAME) {
        current = { ...current, workActs: [...current.workActs, part] };
      }
    }
  }
  close();
  const last = steps.at(-1) ?? null;
  return { earlier: steps.slice(0, -1), last };
}
