import { cn } from "client-core/lib/cn";
import type { ReactElement, ReactNode } from "react";

// Who or what caused a run to move, said as a sentence a person can read
// aloud, never as a color alone. actor picks the icon and its tone; variant
// picks the layout (inline for a footer row, box for a standalone info
// block with a heading). The sentence itself always comes from the
// `provenance` builders below, so every screen says the same thing for the
// same actor.

// `person-pending` is the one actor for a gate nobody has judged yet: it names
// who must act, so a waiting gate never wears a machine's or an approver's mark.
// `person-rejected` names a person's own no, distinct from `person-approved`'s yes.
export type ProvenanceActor =
  | "person-approved"
  | "person-pending"
  | "person-rejected"
  | "agent-decided"
  | "automatic"
  | "schedule-started"
  | "person-started";

export interface ProvenanceChipProps {
  actor: ProvenanceActor;
  variant?: "inline" | "box";
  heading?: string;
  children: ReactNode;
  className?: string;
}

function joinWords(words: readonly string[]): string {
  if (words.length <= 1) {
    return words[0] ?? "";
  }
  if (words.length === 2) {
    return `${words[0]} and ${words[1]}`;
  }
  const last = words[words.length - 1];
  return `${words.slice(0, -1).join(", ")}, and ${last}`;
}

export const provenance = {
  // A name is spoken only when one is on record; an unknown approver is still a
  // person, never a guess such as "you".
  approvedByPerson: (name?: string): string =>
    name ? `Approved by a person, ${name}.` : "Approved by a person.",
  // How many times is the caller's own mono count, kept out of this sentence.
  rejectedByPerson: (): string => "Rejected by a person.",
  decidedByAgent: (gates: readonly string[]): string => {
    const many = gates.length > 1;
    const pronoun = many ? "these" : "this";
    const object = many ? "them" : "it";
    return `${joinWords(gates)} passed. The orchestrator agent decided ${pronoun}, no person approved ${object}.`;
  },
  passedAutomatically: (gates: readonly string[]): string => {
    const many = gates.length > 1;
    const verb = many ? "were" : "was";
    const object = many ? "them" : "it";
    return `${joinWords(gates)} ${verb} configured to pass without review, no person approved ${object}.`;
  },
  startedBySchedule: (name: string): string => `Started by a schedule, ${name}.`,
  startedByPerson: (name?: string): string =>
    name ? `Started by a person, ${name}.` : "Started by a person.",
  startedFromChat: (): string => "Started from chat.",
  // The sentence for a gate nobody has judged yet, paired with the
  // `person-pending` actor.
  needsAPerson: (): string => "This gate needs a person, the agent cannot decide it.",
};

function PersonIcon({ size, className }: { size: number; className: string }): ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className={className}
    >
      <circle cx="8" cy="5.2" r="2.8" />
      <path d="M2.6 14c.6-3 2.8-4.4 5.4-4.4S12.8 11 13.4 14" />
    </svg>
  );
}

function AutomaticIcon({ size, className }: { size: number; className: string }): ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className={className}
    >
      <path d="M8 2v3.6M8 2L5.6 4.4M8 2l2.4 2.4" />
      <rect x="2.6" y="7" width="10.8" height="6.6" />
    </svg>
  );
}

function ScheduleIcon({ size, className }: { size: number; className: string }): ReactElement {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      aria-hidden="true"
      className={className}
    >
      <circle cx="8" cy="8" r="6.4" />
      <path d="M8 4.4V8l2.4 1.6" />
    </svg>
  );
}

// The agent gate hexagon, ink filled so it holds on both themes, with the
// structured judgement grid inverted against the surface token for
// contrast, since a flat white would vanish against the near white ink of
// a dark theme.
function AgentIcon({ size }: { size: number }): ReactElement {
  const height = Math.round((size * 38) / 42);
  return (
    <svg width={size} height={height} viewBox="0 0 42 38" aria-hidden="true">
      <polygon points="10.5,0 31.5,0 42,19 31.5,38 10.5,38 0,19" className="fill-label-1" />
      <svg
        x="13"
        y="11"
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        strokeWidth={1.7}
        className="stroke-surface"
        aria-hidden="true"
      >
        <rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1" />
        <path d="M6.8 1.8v2.6M9.2 1.8v2.6M6.8 11.6v2.6M9.2 11.6v2.6M1.8 6.8h2.6M1.8 9.2h2.6M11.6 6.8h2.6M11.6 9.2h2.6" />
      </svg>
    </svg>
  );
}

function ActorIcon({ actor, size }: { actor: ProvenanceActor; size: number }): ReactElement {
  switch (actor) {
    case "person-approved":
      return <PersonIcon size={size} className="text-label-2" />;
    case "person-pending":
      return <PersonIcon size={size} className="text-warn-ink" />;
    case "person-rejected":
      return <PersonIcon size={size} className="text-warn-ink" />;
    case "person-started":
      return <PersonIcon size={size} className="text-label-3" />;
    case "agent-decided":
      return <AgentIcon size={size} />;
    case "automatic":
      return <AutomaticIcon size={size} className="text-label-2" />;
    case "schedule-started":
      return <ScheduleIcon size={size} className="text-label-3" />;
  }
}

export function ProvenanceChip({
  actor,
  variant = "inline",
  heading,
  children,
  className,
}: ProvenanceChipProps): ReactElement {
  if (variant === "box") {
    return (
      <div
        className={cn(
          "flex items-start gap-3 rounded-lg border border-line bg-well p-3.5",
          className,
        )}
      >
        <ActorIcon actor={actor} size={22} />
        <div className="flex flex-col gap-1">
          {heading ? (
            <p className="font-semibold text-caption text-label-1 tracking-wide">{heading}</p>
          ) : null}
          <p className="text-caption text-label-3">{children}</p>
        </div>
      </div>
    );
  }

  return (
    <div className={cn("flex items-center gap-2 text-caption text-label-2", className)}>
      <ActorIcon actor={actor} size={14} />
      <span>{children}</span>
    </div>
  );
}
