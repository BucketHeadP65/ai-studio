"use client";

import { cn } from "client-core/lib/cn";
import { Reply } from "lucide-react";
import type { ReactElement, ReactNode } from "react";

import type { Blame } from "@/api/inspection";
import { SPAN_TOKEN, citesOf, sentence, spaced, surfaceLabel } from "@/api/inspection-words";

import { rootCauseIcon, surfaceIcon } from "./icons";

// The judgment table, one row per blame: the root cause, where the fix lands, the why with its
// coordinates as links, the spans it cites, and a challenge. Hovering a row is the caller's
// signal to light the spans it cites elsewhere on the page.

const GRID = "grid grid-cols-[24px_270px_270px_minmax(0,1fr)_150px_96px] gap-x-4";

function CoordinateButtons({ text, spans, onPick }: { text: string; spans: string[]; onPick: (span: string) => void }): ReactElement {
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(SPAN_TOKEN)) {
    const token = match[0];
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(text.slice(cursor, index));
    key += 1;
    if (spans.includes(token)) {
      nodes.push(
        <button key={key} type="button" onClick={() => onPick(token)} className="font-mono text-accent underline-offset-2 hover:underline">
          {token}
        </button>,
      );
    } else {
      nodes.push(token);
    }
    cursor = index + token.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return <>{nodes}</>;
}

function JudgmentRow({
  blame,
  index,
  spans,
  onHover,
  onPick,
  onChallenge,
}: {
  blame: Blame;
  index: number;
  spans: string[];
  onHover: (index: number | null) => void;
  onPick: (span: string) => void;
  onChallenge: (index: number) => void;
}): ReactElement {
  const RootCauseIcon = rootCauseIcon(blame.root_cause);
  const SurfaceIcon = surfaceIcon(blame.surface_kind);
  const cited = citesOf(blame.why, spans);

  return (
    <div
      role="row"
      tabIndex={-1}
      onMouseEnter={() => onHover(index)}
      onMouseLeave={() => onHover(null)}
      className={cn(GRID, "items-start px-3.5 py-2.5 hover:bg-well", index > 0 && "border-line border-t")}
    >
      <div role="cell" className="font-mono text-caption text-label-3">
        {index + 1}
      </div>

      <div role="cell" className="flex items-start gap-2">
        <RootCauseIcon size={16} className="mt-0.5 shrink-0 text-label-3" />
        <div>
          <div className="font-semibold text-body">{sentence(spaced(blame.root_cause))}</div>
          <div className="text-caption text-label-2">{blame.root_cause_text}</div>
        </div>
      </div>

      <div role="cell" className="flex items-start gap-2">
        <SurfaceIcon size={16} className="mt-0.5 shrink-0 text-label-3" />
        <div>
          <div className="text-body">
            {sentence(surfaceLabel(blame.surface_kind))}
            {blame.surface_name === "" ? null : <> <span className="font-mono font-semibold">{blame.surface_name}</span></>}
          </div>
          {blame.surface_name === "" ? <div className="text-caption text-label-3">Nothing named to edit</div> : null}
        </div>
      </div>

      <div role="cell" className="text-body text-label-1">
        <CoordinateButtons text={blame.why} spans={spans} onPick={onPick} />
      </div>

      <div role="cell" className="font-mono text-caption">
        {cited.map((token, tokenIndex) => (
          <span key={token}>
            {tokenIndex > 0 ? " " : ""}
            <button type="button" onClick={() => onPick(token)} className="font-mono text-accent underline-offset-2 hover:underline">
              {token}
            </button>
          </span>
        ))}
      </div>

      <div role="cell">
        <button type="button" onClick={() => onChallenge(index)} className="inline-flex items-center gap-1 text-accent">
          <Reply size={14} />
          Challenge
        </button>
      </div>
    </div>
  );
}

export function JudgmentTable({
  blames,
  spans,
  onHover,
  onPick,
  onChallenge,
}: {
  blames: Blame[];
  spans: string[];
  onHover: (index: number | null) => void;
  onPick: (span: string) => void;
  onChallenge: (index: number) => void;
}): ReactElement {
  return (
    <div role="table" aria-label="What is to blame" className="rounded-lg border border-line bg-surface">
      <div role="row" tabIndex={-1} className={cn(GRID, "border-line border-b px-3.5 py-1.5 text-caption text-label-3")}>
        <div role="columnheader" tabIndex={-1} />
        <div role="columnheader" tabIndex={-1}>
          Root cause
        </div>
        <div role="columnheader" tabIndex={-1}>
          Fix lands on
        </div>
        <div role="columnheader" tabIndex={-1}>
          Why
        </div>
        <div role="columnheader" tabIndex={-1}>
          Cites
        </div>
        <div role="columnheader" tabIndex={-1} />
      </div>
      {blames.map((blame, index) => (
        <JudgmentRow
          key={`${index}:${blame.root_cause}`}
          blame={blame}
          index={index}
          spans={spans}
          onHover={onHover}
          onPick={onPick}
          onChallenge={onChallenge}
        />
      ))}
    </div>
  );
}
