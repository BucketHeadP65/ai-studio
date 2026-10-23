"use client";

import { cn } from "@/shared/lib/cn";
import type { ReactElement, ReactNode } from "react";

import type { Evidence } from "@/api/inspection";
import { keyLine, partWords, sentence } from "@/api/inspection-words";

import { Highlighted } from "./highlighted";
import { partIcon } from "./icons";

// The evidence, one card per cited span in transcript order, joined by a thin line: the part's
// own icon, a header naming what it is, the coordinate, and the line that quotes what a blame's
// why quotes (or the span's first line). Clicking a card picks it for the detail pane.

const RETURN_OF = "the return of ";
const CALL_TO = "the model's call to ";

function toolNameFrom(words: string): string {
  if (words.startsWith(RETURN_OF)) return words.slice(RETURN_OF.length);
  if (words.startsWith(CALL_TO)) return words.slice(CALL_TO.length);
  return words;
}

function headerLine(piece: Evidence): ReactNode {
  const words = partWords(piece.label);
  switch (piece.kind) {
    case "tool-return":
      return (
        <>
          <span className="font-mono">{toolNameFrom(words)}</span> returned, {piece.text.split("\n").length} lines
        </>
      );
    case "thinking":
      return "the model thought";
    case "tool-call":
      return (
        <>
          the model called <span className="font-mono">{toolNameFrom(words)}</span>
        </>
      );
    case "text":
      return "the model said";
    case "user-prompt":
      return "the user's prompt";
    default:
      return words;
  }
}

function EvidenceCard({
  piece,
  phrases,
  picked,
  lit,
  onPick,
}: {
  piece: Evidence;
  phrases: string[];
  picked: string;
  lit: Set<string>;
  onPick: (span: string) => void;
}): ReactElement {
  const Icon = partIcon(piece.kind);
  const isPicked = picked === piece.span;
  const isLit = lit.has(piece.span);

  return (
    <button
      type="button"
      aria-label={piece.label}
      onClick={() => onPick(piece.span)}
      className={cn(
        "relative w-full rounded-lg border px-3 py-2.5 pl-9 text-left",
        isPicked ? "bg-accent-soft border-accent" : isLit ? "border-accent" : "border-line bg-surface",
      )}
    >
      {piece.kind === "unreadable" ? (
        <p className="text-caption text-label-3">{sentence(piece.text)}.</p>
      ) : (
        <>
          <Icon size={16} className="absolute top-3 left-3 text-label-3" />
          <div className="flex items-baseline justify-between gap-3 text-caption text-label-2">
            <span>{headerLine(piece)}</span>
            <span className="font-mono text-accent">{piece.span}</span>
          </div>
          <p className="mt-1 font-mono text-caption text-label-1">
            <Highlighted text={keyLine(piece.text, phrases)} phrases={phrases} />
          </p>
        </>
      )}
    </button>
  );
}

export function EvidenceStory({
  evidence,
  phrases,
  picked,
  lit,
  onPick,
}: {
  evidence: Evidence[];
  phrases: string[];
  picked: string;
  lit: Set<string>;
  onPick: (span: string) => void;
}): ReactElement {
  return (
    <div className="flex flex-col">
      {evidence.flatMap((piece, index) => {
        const card = <EvidenceCard key={piece.span} piece={piece} phrases={phrases} picked={picked} lit={lit} onPick={onPick} />;
        return index === 0 ? [card] : [<div key={`${piece.span}-line`} className="ml-5 h-3 border-line border-l" />, card];
      })}
    </div>
  );
}
