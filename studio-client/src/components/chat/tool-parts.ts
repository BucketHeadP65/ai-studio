import { getToolName, isStaticToolUIPart } from "ai";

import { type ToolResult, isConsoleAct, isVerb } from "@/api/page-context";

import type { ToolUIPart, UIMessage } from "ai";

// Reading the companion's tool parts off the wire's own UIMessage stream: what is still
// pending, what the next request must carry.

type ApprovalRequestedPart = Extract<ToolUIPart, { state: "approval-requested" }>;
type ApprovalRespondedPart = Extract<ToolUIPart, { state: "approval-responded" }>;
type InputAvailablePart = Extract<ToolUIPart, { state: "input-available" }>;
type OutputAvailablePart = Extract<ToolUIPart, { state: "output-available" }>;

function isApprovalRequested(part: ToolUIPart): part is ApprovalRequestedPart {
  return part.state === "approval-requested";
}

function isApprovalResponded(part: ToolUIPart): part is ApprovalRespondedPart {
  return part.state === "approval-responded";
}

function isInputAvailable(part: ToolUIPart): part is InputAvailablePart {
  return part.state === "input-available";
}

function isOutputAvailable(part: ToolUIPart): part is OutputAvailablePart {
  return part.state === "output-available";
}

export function lastStepToolParts(message: UIMessage): ToolUIPart[] {
  const lastStepStart = message.parts.reduce(
    (last, part, index) => (part.type === "step-start" ? index : last),
    -1,
  );
  return message.parts.slice(lastStepStart + 1).filter(isStaticToolUIPart);
}

function lastAssistant(messages: UIMessage[]): UIMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === "assistant") return message;
    if (message?.role === "user" && index !== messages.length - 1) return null;
  }
  return null;
}

export interface PendingApproval {
  approvalId: string;
  toolCallId: string;
  toolName: string;
  input: unknown;
}

// Only the last message being the assistant's own counts as still pending -- once the
// person has answered (their message rides last), nothing here is outstanding anymore.
export function pendingApprovals(messages: UIMessage[]): PendingApproval[] {
  const last = messages.at(-1);
  if (last?.role !== "assistant") return [];
  return lastStepToolParts(last)
    .filter(isApprovalRequested)
    .map((part) => ({
      approvalId: part.approval.id,
      toolCallId: part.toolCallId,
      toolName: getToolName(part),
      input: part.input,
    }));
}

export function unresolvedVerbs(messages: UIMessage[]): ToolUIPart[] {
  const last = messages.at(-1);
  if (last?.role !== "assistant") return [];
  return lastStepToolParts(last)
    .filter(isInputAvailable)
    .filter((part) => isVerb(getToolName(part)));
}

// What the next request reports back: a verb's own outcome, or the person's answer to
// an approval request -- read off the last assistant message even when the person has
// since typed a reply (a supersede still needs its denials reported).
//
// `settled` names the calls this browser answered, and only a verb outcome under one of
// those ids is reported: a transcript read back from storage brings every companion call
// back as an output-available part, carrying its stored return or a pending marker where
// it has none, and neither is an answer this browser gave. An approval answer needs no
// such gate, since only answering one here ever puts a part in that state.
export function toolResultsOf(messages: UIMessage[], settled: ReadonlySet<string>): ToolResult[] {
  const assistantMessage = lastAssistant(messages);
  if (assistantMessage === null) return [];
  const results: ToolResult[] = [];
  for (const part of lastStepToolParts(assistantMessage)) {
    const toolName = getToolName(part);
    if (isVerb(toolName) && isOutputAvailable(part) && settled.has(part.toolCallId)) {
      results.push({
        tool_call_id: part.toolCallId,
        tool_name: toolName,
        output: String(part.output),
      });
    } else if (isApprovalResponded(part)) {
      const result: ToolResult = {
        tool_call_id: part.toolCallId,
        tool_name: toolName,
        approved: part.approval.approved,
      };
      if (part.approval.reason !== undefined) {
        result.reason = part.approval.reason;
      }
      results.push(result);
    }
  }
  return results;
}

// Whether any console act reached an outcome in this turn. The whole message is the
// window here, not its last step: the stream closes a step and opens another before
// every model response after the first, so an act that ran early sits a step or more
// behind the turn's closing text.
export function hasActOutcome(message: UIMessage): boolean {
  return message.parts.filter(isStaticToolUIPart).some((part) => {
    if (!isConsoleAct(getToolName(part))) return false;
    return part.state === "output-available" || part.state === "output-denied";
  });
}
