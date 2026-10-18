"use client";

import { getToolName } from "ai";
import { cn } from "client-core/lib/cn";
import { CheckCircle2, ChevronDown, Clock, XCircle } from "lucide-react";
import { type ReactElement, type ReactNode, useEffect, useRef, useState } from "react";

import { Tool, ToolContent } from "@/components/ai-elements/tool";
import { compactCount } from "@/components/follow-me/vitals";
import { CollapsibleTrigger } from "@/components/ui/collapsible";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";

import { DiffView } from "./diff-view";
import { JsonTree, LONG_STRING_THRESHOLD } from "./json-tree";
import { readableToolText } from "./reply-text";
import { TextViewer } from "./text-viewer";
import { formatToolElapsed, presentTool, stringField, summarizeResult } from "./tool-presentation";

import type { ToolFacts } from "./step-usage";
import type { ActPart } from "./turn-fold";

// One work tool call as a collapsible card. Closed, it is one row: where the call stands, what
// it did (the verb line, the raw tool name muted beside it), what came of it (the summary), and
// how long it took and what it cost in tokens. A finished call says so with a mark alone; only a
// call still running, waiting, refused or failed says so in words, and a running call's own mark
// ticks off the moment the card first saw it. Open, the body holds the call's parameters and its
// result or error, values rendered the way the inspector renders them, a collapsible tree for
// structured data and wrapped text for strings, inside a region capped in height that scrolls; a
// long string opens on its own in the viewer. While the call's arguments are still streaming the
// part carries no input yet, so the card shows an empty parameter set rather than none.

type SettledState = Extract<
  ActPart["state"],
  "output-available" | "output-error" | "output-denied"
>;

function isSettled(state: ActPart["state"]): state is SettledState {
  return state === "output-available" || state === "output-error" || state === "output-denied";
}

function Status({
  state,
  elapsedMs,
}: { state: ActPart["state"]; elapsedMs: number }): ReactElement {
  switch (state) {
    case "output-available":
      return (
        <CheckCircle2
          size={14}
          strokeWidth={1.75}
          aria-label="Completed"
          className="shrink-0 text-label-3"
        />
      );
    case "output-error":
      return (
        <span className="flex shrink-0 items-center gap-1 text-caption text-danger-ink">
          <XCircle size={14} strokeWidth={1.75} aria-hidden />
          Error
        </span>
      );
    case "output-denied":
      return <span className="shrink-0 text-caption text-warn-ink">Denied</span>;
    case "approval-requested":
      return <span className="shrink-0 text-caption text-warn-ink">Awaiting approval</span>;
    case "approval-responded":
      return <span className="shrink-0 text-caption text-label-3">Responded</span>;
    default:
      return (
        <span className="flex shrink-0 items-center gap-1 text-caption text-label-3">
          <Clock
            size={14}
            strokeWidth={1.75}
            aria-hidden
            data-motion="essential"
            className="animate-pulse"
          />
          {`Running ${formatElapsedMs(elapsedMs)}`}
        </span>
      );
  }
}

function Section({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div className="flex flex-col gap-1.5">
      <h4 className="font-medium text-label-3 text-micro uppercase tracking-caps">{label}</h4>
      {children}
    </div>
  );
}

function Value({
  value,
  onView,
}: { value: unknown; onView: (text: string) => void }): ReactElement {
  if (typeof value !== "string") {
    return <JsonTree value={value} onView={onView} />;
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <pre className="whitespace-pre-wrap break-words font-mono text-caption text-label-2">
        {value}
      </pre>
      {value.length > LONG_STRING_THRESHOLD ? (
        <button
          type="button"
          onClick={() => onView(value)}
          className="text-accent text-caption underline"
        >
          View
        </button>
      ) : null}
    </div>
  );
}

function EditParameters({
  path,
  input,
  onView,
}: { path: string | null; input: unknown; onView: (text: string) => void }): ReactElement {
  const oldText = stringField(input, "old_text");
  const newText = stringField(input, "new_text");
  return (
    <>
      {path !== null ? <p className="font-mono text-caption text-label-3">{path}</p> : null}
      {oldText !== undefined && newText !== undefined ? (
        <DiffView oldText={oldText} newText={newText} />
      ) : (
        <Value value={input ?? {}} onView={onView} />
      )}
    </>
  );
}

function WriteParameters({
  input,
  onView,
}: { input: unknown; onView: (text: string) => void }): ReactElement {
  const content = stringField(input, "content");
  if (content === undefined) {
    return <Value value={input ?? {}} onView={onView} />;
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <pre className="whitespace-pre-wrap break-words font-mono text-caption text-label-2">
        {content}
      </pre>
      <p className="text-caption text-label-3">{content.split("\n").length} lines</p>
    </div>
  );
}

// The settled duration in parentheses beside the tokens spent, `(0.7s, ↓ 51 tokens)` -- server
// timestamps when the facts carry both ends of the call, the card's own clock when it watched
// the call finish live, and the tokens whenever the step's facts are known. `null` with nothing
// to say, so a settled call with neither reading carries no parentheses at all.
function settledDetail(facts: ToolFacts | undefined, watchedMs: number | null): string | null {
  const durationMs =
    facts !== undefined && facts.returnedAt !== null
      ? Date.parse(facts.returnedAt) - Date.parse(facts.startedAt)
      : watchedMs;
  const parts = [
    durationMs === null ? null : formatToolElapsed(durationMs),
    facts === undefined ? null : `↓ ${compactCount(facts.tokensOut)} tokens`,
  ].filter((value) => value !== null);
  return parts.length === 0 ? null : `(${parts.join(", ")})`;
}

export function WorkTool({
  part,
  facts,
}: { part: ActPart; facts: ToolFacts | undefined }): ReactElement {
  const name = getToolName(part);
  const [viewed, setViewed] = useState<string | null>(null);
  const view = (text: string): void => setViewed(readableToolText(text));

  const settled = isSettled(part.state);
  const [seenAt] = useState(() => Date.now());
  const [finishedAt, setFinishedAt] = useState<number | null>(null);
  const previousState = useRef(part.state);
  useEffect(() => {
    if (!isSettled(previousState.current) && isSettled(part.state)) {
      setFinishedAt(Date.now());
    }
    previousState.current = part.state;
  }, [part.state]);
  const now = useNowTick(1000, !settled);

  const presentation = presentTool(name, part.input);
  const line =
    presentation.subject === null
      ? presentation.verb
      : `${presentation.verb} ${presentation.subject}`;
  const summary =
    part.state === "output-available" ? summarizeResult(name, part.input, part.output) : null;
  const detail = settled
    ? settledDetail(facts, finishedAt === null ? null : finishedAt - seenAt)
    : null;

  return (
    <Tool className="mb-0 w-full bg-well/60">
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-3 py-1.5 text-left">
        <Status state={part.state} elapsedMs={now - seenAt} />
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-caption text-label-1",
            presentation.kind === "execute" && "font-mono",
          )}
        >
          {line}
          {line !== name ? (
            <span className="ml-2 font-mono text-label-3 text-micro">{name}</span>
          ) : null}
        </span>
        {summary !== null ? (
          <span className="shrink-0 text-caption text-label-3">{summary}</span>
        ) : null}
        {detail !== null ? (
          <span className="shrink-0 text-caption text-label-3">{detail}</span>
        ) : null}
        <ChevronDown
          size={14}
          strokeWidth={1.75}
          aria-hidden
          className="shrink-0 text-label-3 transition-transform group-data-[state=open]:rotate-180"
        />
      </CollapsibleTrigger>
      <ToolContent className="max-h-80 space-y-3 overflow-y-auto p-3">
        <Section label="Parameters">
          {name === "write_file" ? (
            <WriteParameters input={part.input} onView={view} />
          ) : presentation.kind === "edit" ? (
            <EditParameters path={presentation.subject} input={part.input} onView={view} />
          ) : (
            <Value value={part.input ?? {}} onView={view} />
          )}
        </Section>
        {part.state === "output-available" ? (
          <Section label="Result">
            <Value value={part.output} onView={view} />
          </Section>
        ) : null}
        {part.state === "output-error" ? (
          <Section label="Error">
            <p className="whitespace-pre-wrap text-caption text-danger-ink">{part.errorText}</p>
          </Section>
        ) : null}
      </ToolContent>
      <TextViewer
        open={viewed !== null}
        title={`${name} value`}
        text={viewed ?? ""}
        mode="plain"
        onClose={() => setViewed(null)}
      />
    </Tool>
  );
}
