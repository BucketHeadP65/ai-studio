import { getToolName } from "ai";
import type { ReactElement } from "react";

import { isCompanionTool } from "@/api/page-context";
import { NEVER_ANSWERED } from "@/api/sessions";

import type { DynamicToolUIPart, ToolUIPart } from "ai";

// One companion tool call read straight off the transcript: which act it is, and where it
// stands. Renders nothing for a tool this surface never acts on, so a caller can map every
// tool part in a message through this without first sorting the companion ones out.
//
// A call the transcript holds no return for reads as lost rather than as done: the browser that
// owed it an answer is gone, and asking again is the only way to get the act run.

function withReason(base: string, reason: string | undefined): string {
  return reason === undefined ? base : `${base}, ${reason}`;
}

function textFor(part: ToolUIPart | DynamicToolUIPart): string {
  const name = getToolName(part);
  switch (part.state) {
    case "input-streaming":
    case "input-available":
      return `${name}, pending`;
    case "approval-requested":
      return `${name}, waiting for your approval`;
    case "approval-responded":
      return withReason(
        `${name}, ${part.approval.approved ? "approved" : "denied"}`,
        part.approval.reason,
      );
    case "output-denied":
      return withReason(`${name}, denied`, part.approval?.reason);
    case "output-error":
      return `${name} failed: ${part.errorText}`;
    case "output-available":
      return part.output === NEVER_ANSWERED
        ? `${name}, left waiting and never ran`
        : `${name}: ${String(part.output)}`;
    default:
      return name;
  }
}

export interface ActLineProps {
  part: ToolUIPart | DynamicToolUIPart;
}

export function ActLine({ part }: ActLineProps): ReactElement | null {
  if (!isCompanionTool(getToolName(part))) {
    return null;
  }
  return <p className="font-mono text-caption text-label-3">{textFor(part)}</p>;
}
