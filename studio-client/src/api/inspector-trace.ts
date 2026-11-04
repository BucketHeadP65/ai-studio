import type { MessagePayload } from "./sessions-types";

// What the header of the inspector's conversation counts: its reads, its refusals and its
// follow-ups.

export interface TraceCounts {
  /** Every tool call except an answer, an answer being a call to a `final_result` output tool. */
  reads: number;
  /** Every return that asked for its call again, and every retry feedback part. */
  refusals: number;
  /** Every user prompt after the first, the first being what the inspector was told. */
  followUps: number;
}

export function traceCounts(messages: MessagePayload[]): TraceCounts {
  let reads = 0;
  let refusals = 0;
  let prompts = 0;
  for (const message of messages) {
    for (const part of message.parts) {
      if (part.part_kind === "tool-call" && !(part.tool_name ?? "").startsWith("final_result")) {
        reads += 1;
      } else if ((part.part_kind === "tool-return" && part.outcome === "retried") || part.part_kind === "retry-feedback") {
        refusals += 1;
      } else if (part.part_kind === "user-prompt") {
        prompts += 1;
      }
    }
  }
  return { reads, refusals, followUps: Math.max(prompts - 1, 0) };
}
