import { z } from "zod";

import { UsageTotalsSchema } from "@/api/sessions-types";

import type { UIMessage } from "ai";

// One model response's own step, the chat stream's `data-step` part and the shape the transcript
// converter rebuilds from stored rows: which tool calls it made, what it spent, and when it ran.
// `returns` names when each of those calls came back, keyed by call id, present only for a call
// this step's own request row already carries a return for.

export const StepUsageSchema = z.object({
  tool_call_ids: z.array(z.string()),
  usage: UsageTotalsSchema,
  timestamp: z.string().datetime({ offset: true }),
  returns: z.record(z.string(), z.string().datetime({ offset: true })).optional(),
});
export type StepUsage = z.infer<typeof StepUsageSchema>;

// One tool call's own facts, read off the step that made it: the response's own output tokens
// (every call the step made shares that figure, since the response spent them making all of
// them at once), when the step ran, and when its call came back -- `null` while it hasn't.
export interface ToolFacts {
  tokensOut: number;
  startedAt: string;
  returnedAt: string | null;
}

// A message's own `data-step` parts, in order. A part whose data does not match the contract is
// skipped and logged, the same posture `settleFromMessage` takes on a broken `data-settle` part:
// one bad chunk never loses the rest of the turn.
export function stepUsages(message: UIMessage): StepUsage[] {
  const steps: StepUsage[] = [];
  for (const part of message.parts) {
    if (part.type !== "data-step") {
      continue;
    }
    const parsed = StepUsageSchema.safeParse(part.data);
    if (!parsed.success) {
      console.error("a data-step part did not match the step contract", parsed.error);
      continue;
    }
    steps.push(parsed.data);
  }
  return steps;
}

// The turn's own output tokens so far, summed across every step. `null` when the message
// carries no step at all, so a caller can tell "spent nothing yet" from "not tracked".
export function turnTokensOut(message: UIMessage): number | null {
  const steps = stepUsages(message);
  if (steps.length === 0) {
    return null;
  }
  return steps.reduce((total, step) => total + step.usage.output_tokens, 0);
}

// Every tool call's own facts, read off the message's steps.
export function toolFacts(message: UIMessage): Map<string, ToolFacts> {
  const facts = new Map<string, ToolFacts>();
  for (const step of stepUsages(message)) {
    for (const toolCallId of step.tool_call_ids) {
      facts.set(toolCallId, {
        tokensOut: step.usage.output_tokens,
        startedAt: step.timestamp,
        returnedAt: step.returns?.[toolCallId] ?? null,
      });
    }
  }
  return facts;
}
