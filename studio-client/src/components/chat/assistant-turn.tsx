"use client";

import type { UIMessage } from "ai";
import { ChevronDown, ChevronRight, ListTree } from "lucide-react";
import { type ReactElement, useId, useMemo, useState } from "react";

import { AgentMarkdown } from "@/components/conversation/markdown";
import { NodeGlyph } from "@/components/marks/node-glyph";

import { ActLine } from "./act-line";
import { type ToolFacts, toolFacts, turnTokensOut } from "./step-usage";
import { ThinkingMark } from "./thinking-mark";
import { pluralVerb } from "./tool-presentation";
import { type ActPart, type TurnStep, foldTurn } from "./turn-fold";
import { type WorkGroup, groupWorkActs } from "./work-groups";
import { WorkTool } from "./work-tool";

// One assistant turn as one bubble. The body is the turn's last step, what the agent said
// once it was done acting. Every earlier step, the narration it gave before each act and the
// act itself, folds into one line above the body that opens in place; a step's narration and
// act lines open the turn inspector at this turn, while its work tool calls are cards with
// their own open-in-place trigger (a work-only step carries no inspector affordance of its
// own — the bubble's "View turn detail" covers it). While the turn is still streaming the
// body is whatever the agent said last, so the person follows the work through the bubble
// itself, cards live in the body while the turn acts.

function WorkGroupRow({
  group,
  facts,
  live,
}: { group: WorkGroup; facts: Map<string, ToolFacts>; live: boolean }): ReactElement {
  const [open, setOpen] = useState(live);
  const listId = useId();
  if (group.parts.length === 1) {
    const [only] = group.parts;
    return <WorkTool part={only} facts={facts.get(only.toolCallId)} />;
  }
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="flex w-full flex-col items-start gap-1">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-1 rounded text-caption text-label-3 transition-colors hover:text-label-1"
      >
        <Chevron size={12} strokeWidth={1.75} aria-hidden />
        {pluralVerb(group.kind, group.parts.length)}
      </button>
      {open ? (
        <div id={listId} className="flex w-full flex-col gap-1.5">
          {group.parts.map((part) => (
            <WorkTool key={part.toolCallId} part={part} facts={facts.get(part.toolCallId)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function WorkActs({
  parts,
  facts,
  live,
}: { parts: ActPart[]; facts: Map<string, ToolFacts>; live: boolean }): ReactElement {
  const groups = groupWorkActs(parts);
  return (
    <>
      {groups.map((group) => (
        <WorkGroupRow key={group.parts[0].toolCallId} group={group} facts={facts} live={live} />
      ))}
    </>
  );
}

function foldLabel(count: number): string {
  return `${count} earlier ${count === 1 ? "step" : "steps"}`;
}

function EarlierStep({
  step,
  onOpen,
  facts,
}: { step: TurnStep; onOpen: () => void; facts: Map<string, ToolFacts> }): ReactElement {
  return (
    <li className="flex flex-col gap-1">
      {step.text !== "" || step.acts.length > 0 ? (
        <button
          type="button"
          onClick={onOpen}
          className="flex w-full flex-col items-start gap-0.5 rounded text-left transition-colors hover:bg-well"
        >
          {step.text !== "" ? (
            <p className="whitespace-pre-wrap text-body text-label-3">{step.text}</p>
          ) : null}
          {step.acts.map((part) => (
            <ActLine key={part.toolCallId} part={part} />
          ))}
        </button>
      ) : null}
      <WorkActs parts={step.workActs} facts={facts} live={false} />
    </li>
  );
}

export function AssistantTurn(props: {
  message: UIMessage;
  /** Absent, no "View turn detail" button renders -- a caller with nothing for it to open. */
  onInspect?: (message: UIMessage) => void;
  /** Who this bubble speaks as. Defaults to the chat station's own orchestrator label. */
  speaker?: string;
  /** The turn is still streaming: every step stays in view in order, and the thinking mark shows. */
  live?: boolean;
  /** When the live turn started, ISO; absent or `null`, the thinking mark shows the verb alone. */
  turnStartedAt?: string | null;
  /** A settled turn starts with its earlier steps folded; off, they stay in view. */
  foldSettled?: boolean;
}): ReactElement {
  const {
    message,
    onInspect,
    speaker = "Orchestrator agent",
    live = false,
    turnStartedAt = null,
    foldSettled = false,
  } = props;
  // `null` until the reader toggles the fold by hand; until then the displayed state tracks
  // `foldSettled` on every render, so a preference read in after the first paint still applies.
  const [override, setOverride] = useState<boolean | null>(null);
  const listId = useId();
  const { earlier, last } = useMemo(() => foldTurn(message), [message]);
  const facts = useMemo(() => toolFacts(message), [message]);
  const tokensOut = useMemo(() => turnTokensOut(message), [message]);
  const open = override ?? !foldSettled;
  const expanded = live || open;
  const Chevron = open ? ChevronDown : ChevronRight;
  // An earlier step's own row opens the turn inspector too; with no inspector to open, it is a
  // no-op rather than a crash on an absent `onInspect`.
  const inspect = onInspect ?? ((): void => {});

  return (
    <div className="flex items-start gap-2.5">
      <NodeGlyph kind="agent-gate" fill="ran" size={30} />
      <div className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-tl-sm rounded-tr-lg rounded-br-lg rounded-bl-lg border border-line bg-surface px-3.5 py-3 text-left shadow-sm">
        <div className="flex w-full items-center justify-between gap-2">
          <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">
            {speaker}
          </p>
          {onInspect ? (
            <button
              type="button"
              onClick={() => onInspect(message)}
              aria-label="View turn detail"
              className="flex items-center gap-1 rounded text-caption text-label-3 transition-colors hover:text-label-1"
            >
              <ListTree size={12} strokeWidth={1.75} aria-hidden />
              View turn detail
            </button>
          ) : null}
        </div>
        {earlier.length > 0 ? (
          <div className="mb-2 flex w-full flex-col items-start gap-1">
            {live ? null : (
              <button
                type="button"
                aria-expanded={open}
                aria-controls={open ? listId : undefined}
                onClick={() => setOverride(!open)}
                className="flex items-center gap-1 rounded text-body text-label-3 transition-colors hover:text-label-1"
              >
                <Chevron size={12} strokeWidth={1.75} aria-hidden />
                {foldLabel(earlier.length)}
              </button>
            )}
            {expanded ? (
              <ol
                id={listId}
                className="ml-1.5 flex w-full flex-col gap-1.5 border-line border-l pl-3"
              >
                {earlier.map((step) => (
                  <EarlierStep
                    key={step.key}
                    step={step}
                    onOpen={() => inspect(message)}
                    facts={facts}
                  />
                ))}
              </ol>
            ) : null}
          </div>
        ) : null}
        <AgentMarkdown text={last?.text ?? ""} />
        {(last?.acts ?? []).map((part) => (
          <ActLine key={part.toolCallId} part={part} />
        ))}
        <WorkActs parts={last?.workActs ?? []} facts={facts} live={live} />
        {live ? <ThinkingMark startedAt={turnStartedAt} tokensOut={tokensOut} /> : null}
      </div>
    </div>
  );
}
