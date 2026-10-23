"use client";

import { cn } from "@/shared/lib/cn";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { ReactElement } from "react";

import type { Evidence } from "@/api/inspection";
import { sentence, spanStep } from "@/api/inspection-words";

import { FOLD_FADE_CLASS, useFolded } from "./folded";
import { Highlighted } from "./highlighted";

// The picked span, in full: the tree rendered in mono with the quoted phrases lit, folded past
// 20 lines one line at a time so each can carry its own highlight. Steps to the next or
// previous span, wrapping around, and opens the step drawer on the picked span's own step. No
// evidence at all (a finding with nothing cited) reads as a plain sentence, no controls.

// The fold belongs to one span's own text -- keyed by `piece.span` in the parent so picking a
// different span remounts this and starts folded again, even when picking back to a span seen before.
function SpanBody({
  piece,
  phrases,
  onReadStep,
}: {
  piece: Evidence;
  phrases: string[];
  onReadStep: (step: number) => void;
}): ReactElement {
  const folded = useFolded(piece.text, 20);

  return (
    <>
      <div className="p-3 font-mono text-caption text-label-1 whitespace-pre-wrap break-words">
        <div className={cn(folded.overflow && !folded.open && FOLD_FADE_CLASS)}>
          {folded.shownLines.map((line, lineIndex) => (
            <div key={lineIndex}>
              <Highlighted text={line} phrases={phrases} />
            </div>
          ))}
        </div>
        {folded.overflow ? (
          <button type="button" onClick={folded.toggle} className="mt-1 text-caption text-label-3 underline">
            {folded.label}
          </button>
        ) : null}
      </div>
      <div className="border-line border-t px-3 py-1.5 text-caption">
        <button type="button" onClick={() => onReadStep(spanStep(piece.span))}>
          Read step {spanStep(piece.span)} in the run
        </button>
      </div>
    </>
  );
}

export function SpanPane({
  evidence,
  phrases,
  picked,
  onPick,
  onReadStep,
}: {
  evidence: Evidence[];
  phrases: string[];
  picked: string;
  onPick: (span: string) => void;
  onReadStep: (step: number) => void;
}): ReactElement {
  const index = Math.max(0, evidence.findIndex((item) => item.span === picked));
  const piece: Evidence | undefined = evidence[index];

  if (piece === undefined) {
    return (
      <div className="flex min-w-0 flex-col self-stretch rounded-lg border border-line bg-surface">
        <p className="p-3 text-body text-label-3">There is no evidence to show.</p>
      </div>
    );
  }

  function step(delta: number): void {
    const next = (index + delta + evidence.length) % evidence.length;
    onPick(evidence[next].span);
  }

  return (
    <div className="flex min-w-0 flex-col self-stretch rounded-lg border border-line bg-surface">
      <div className="flex items-center justify-between border-line border-b bg-well px-3 py-2 text-caption text-label-2">
        <span>{piece.label}</span>
        <div className="flex items-center gap-2.5">
          <button type="button" aria-label="Previous span" onClick={() => step(-1)}>
            <ChevronLeft size={14} />
          </button>
          <span className="font-mono">
            {index + 1} of {evidence.length}
          </span>
          <button type="button" aria-label="Next span" onClick={() => step(1)}>
            <ChevronRight size={14} />
          </button>
          <span className="font-mono text-accent">{piece.span}</span>
        </div>
      </div>
      {piece.kind === "unreadable" ? (
        <p className="p-3 text-caption text-label-3">{sentence(piece.text)}.</p>
      ) : (
        <SpanBody key={piece.span} piece={piece} phrases={phrases} onReadStep={onReadStep} />
      )}
    </div>
  );
}
