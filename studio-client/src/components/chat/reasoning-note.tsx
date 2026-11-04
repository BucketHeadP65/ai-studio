import type { UIMessage } from "ai";
import type { ReactElement } from "react";

import { AgentMarkdown } from "@/components/conversation/markdown";

// A step's thinking, drawn ahead of its turn bubble: `AssistantTurn` draws no reasoning part, so
// a surface that shows a step's thinking draws this beside it. Nothing renders for a message with
// no reasoning part. `live` says the message is still streaming in, the same flag its turn takes.

type ReasoningPart = Extract<UIMessage["parts"][number], { type: "reasoning" }>;

function isReasoningPart(part: UIMessage["parts"][number]): part is ReasoningPart {
  return part.type === "reasoning";
}

export function ReasoningNote({ message, live }: { message: UIMessage; live: boolean }): ReactElement | null {
  const reasoning = message.parts.filter(isReasoningPart);
  if (reasoning.length === 0) return null;
  return (
    <div className="flex w-full flex-col gap-1 rounded-md border border-line bg-well px-3 py-2">
      <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">Thinking</p>
      {reasoning.map((part, index) => (
        <AgentMarkdown key={`${message.id}-reasoning-${index}`} text={part.text} live={live} />
      ))}
    </div>
  );
}
