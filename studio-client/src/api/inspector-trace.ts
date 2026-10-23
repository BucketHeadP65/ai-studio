import type { MessagePart, MessagePayload } from "./sessions-types";
import { argsRecord, callLine, treeText } from "./tree-text";

// The inspector's conversation read as a trace, not a chat: what it was told, what it thought, what it called
// and what came back, what it said, the refusal, the answer, and any later question. A tool's return is matched
// to its call by `tool_call_id` and travels with it, never at its own position in the stream.

export type TraceEntry =
  | { kind: "told"; text: string }
  | { kind: "call"; name: string; line: string; returned: string | null }
  | { kind: "thinking"; text: string }
  | { kind: "said"; text: string }
  | { kind: "refused"; text: string; rejected: Record<string, unknown> | null }
  | { kind: "answered"; args: Record<string, unknown>; outcome?: "finding" | "dismissed" | "clean" | "undecided" | "unvalidated" }
  | { kind: "asked"; text: string };

export function plainText(content: unknown): string {
  if (typeof content === "string") return content;
  return content === null || content === undefined ? "" : String(content);
}

export function errorItemText(item: unknown): string {
  if (item !== null && typeof item === "object" && "msg" in item) {
    const msg = (item as { msg: unknown }).msg;
    if (typeof msg === "string") return msg;
  }
  return typeof item === "string" ? item : JSON.stringify(item);
}

// A string, or a Pydantic error list joined with ", ".
export function retryText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map(errorItemText).join(", ");
  return plainText(content);
}

// Whether a part is a call's return asking the model to make the call again.
function isRetriedReturn(part: MessagePart): boolean {
  return part.part_kind === "tool-return" && part.outcome === "retried";
}

// A tool's return by its call's `tool_call_id`, and the `tool_call_id` of every call a retried
// return names as rejected -- both read in the same pass over the conversation, ahead of the
// entries themselves, since a call's return can land several parts later.
export function toolReturnsByCallId(messages: MessagePayload[]): { returns: Map<string, unknown>; rejectedCallIds: Set<string> } {
  const returns = new Map<string, unknown>();
  const rejectedCallIds = new Set<string>();
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.part_kind !== "tool-return" || part.tool_call_id === undefined) {
        continue;
      }
      if (isRetriedReturn(part)) {
        rejectedCallIds.add(part.tool_call_id);
      } else {
        returns.set(part.tool_call_id, part.content);
      }
    }
  }
  return { returns, rejectedCallIds };
}

export function traceEntries(messages: MessagePayload[]): TraceEntry[] {
  const { returns, rejectedCallIds } = toolReturnsByCallId(messages);
  const entries: TraceEntry[] = [];
  // A rejected `final_result_*` call's own args, by its id -- filled in as the call is walked,
  // read back once the retried return naming it follows.
  const rejectedArgsByCallId = new Map<string, Record<string, unknown>>();
  let sawFirstPrompt = false;
  for (const message of messages) {
    for (const part of message.parts) {
      switch (part.part_kind) {
        case "user-prompt": {
          const text = plainText(part.content);
          entries.push(sawFirstPrompt ? { kind: "asked", text } : { kind: "told", text });
          sawFirstPrompt = true;
          break;
        }
        case "system-prompt":
          entries.push({ kind: "told", text: plainText(part.content) });
          break;
        case "thinking":
          entries.push({ kind: "thinking", text: plainText(part.content) });
          break;
        case "text":
          entries.push({ kind: "said", text: plainText(part.content) });
          break;
        case "tool-call": {
          const name = part.tool_name ?? "tool";
          const args = argsRecord(part.args);
          if (name.startsWith("final_result")) {
            if (part.tool_call_id !== undefined && rejectedCallIds.has(part.tool_call_id)) {
              rejectedArgsByCallId.set(part.tool_call_id, args);
              break;
            }
            // Other successful output-tool returns can acknowledge skipped, unvalidated outputs.
            const accepted = part.tool_call_id !== undefined && returns.get(part.tool_call_id) === "Final result processed.";
            const outcome = !accepted ? "unvalidated" : name.endsWith("NoIssue") ? "dismissed"
              : name.endsWith("NothingSeen") ? "clean"
              : name.endsWith("Undecided") ? "undecided" : "finding";
            entries.push({ kind: "answered", args, outcome });
            break;
          }
          const returned = part.tool_call_id !== undefined && returns.has(part.tool_call_id) ? treeText(returns.get(part.tool_call_id)) : null;
          entries.push({ kind: "call", name, line: callLine(name, args), returned });
          break;
        }
        case "tool-return": {
          if (isRetriedReturn(part)) {
            const rejected = part.tool_call_id !== undefined ? (rejectedArgsByCallId.get(part.tool_call_id) ?? null) : null;
            entries.push({ kind: "refused", text: retryText(part.content), rejected });
          }
          break;
        }
        case "retry-feedback":
          entries.push({ kind: "refused", text: retryText(part.content), rejected: null });
          break;
        default:
          break;
      }
    }
  }
  return entries;
}

export function traceCounts(entries: TraceEntry[]): { reads: number; refusals: number; followUps: number } {
  return {
    reads: entries.filter((entry) => entry.kind === "call").length,
    refusals: entries.filter((entry) => entry.kind === "refused").length,
    followUps: entries.filter((entry) => entry.kind === "asked").length,
  };
}
