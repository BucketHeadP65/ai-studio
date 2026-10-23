"use client";

import type { ReactElement, ReactNode } from "react";

import type { StepOutline } from "@/api/inspection";
import { plural } from "@/api/inspection-words";
import { cn } from "@/shared/lib/cn";
import type { StepMark } from "./run-overview-facts";

// The run's steps as one bar per step, width by how many messages it added: the accent for a
// step a finding cites, the accent dimmed for a step a lead was dismissed on, the line tone for
// the rest. The selected step is ringed. Clicking a bar picks that step.

function tone(mark: StepMark | undefined): string {
  if (mark === "finding") return "bg-accent";
  if (mark === "dismissed") return "bg-accent/40";
  if (mark === "unresolved") return "bg-danger-ink/50";
  return "bg-line";
}

export function RunTimeline({
  steps,
  marked,
  selected,
  onSelect,
  caption,
}: {
  steps: StepOutline[];
  marked: Map<number, StepMark>;
  selected: number | null;
  onSelect: (step: number) => void;
  caption?: ReactNode;
}): ReactElement {
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <div className="flex gap-0.5">
        {steps.map(({ step, messages, summary }) => (
          <button
            key={step}
            type="button"
            aria-label={`step ${step}, ${plural(messages, "message", "messages")}`}
            title={`step ${step}, ${plural(messages, "message", "messages")}, ${summary}`}
            onClick={() => onSelect(step)}
            style={{ flex: `${Math.max(messages, 1)} 0 0` }}
            className={cn(
              "h-2.5 min-w-3 rounded-sm",
              tone(marked.get(step)),
              selected === step && "outline outline-2 outline-label-1 outline-offset-2",
            )}
          />
        ))}
      </div>
      <div className="mt-1 flex gap-0.5">
        {steps.map(({ step, messages }) => (
          <div key={step} style={{ flex: `${Math.max(messages, 1)} 0 0` }} className="text-center font-mono text-label-3 text-micro">
            {step}
          </div>
        ))}
      </div>
      {caption}
    </div>
  );
}
