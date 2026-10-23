"use client";

import { type ReactElement, useState } from "react";

import { cn } from "@/shared/lib/cn";

// A block of text that folds past a line count, once, no animation (the owner's Reduce Motion
// is always on). A caller decides `mono` by what the text is, not by taste. Folded, past its
// line count, is cut by its own box, never by an ellipsis character; `fade` lightens the cut
// with a bottom mask instead of a hard edge.

export const FOLD_FADE_CLASS = "overflow-hidden [mask-image:linear-gradient(to_bottom,black_75%,transparent)]";

export interface FoldState {
  shownLines: string[];
  overflow: boolean;
  open: boolean;
  toggle: () => void;
  label: string;
}

// The fold state and words a block of text uses when it folds past a line count: which lines
// to show, whether it can fold further, and the toggle's own copy. Shared by `Folded` and any
// caller that must render the folded lines itself rather than as one plain block (the span pane
// highlights each line).
export function useFolded(text: string, lines: number): FoldState {
  const [open, setOpen] = useState(false);
  const allLines = text.split("\n");
  const overflow = allLines.length > lines;
  const shownLines = open || !overflow ? allLines : allLines.slice(0, lines);
  const label = open ? `Show the first ${lines} lines` : `Show all (${allLines.length} lines)`;
  return { shownLines, overflow, open, toggle: () => setOpen((value) => !value), label };
}

export interface FoldedProps {
  text: string;
  lines?: number;
  mono?: boolean;
  fade?: boolean;
  // Overrides the toggle's own words -- `closed` shown while folded, `open` shown while
  // expanded -- for a caller whose fold reads better than "Show all (N lines)".
  words?: { open: string; closed: string };
}

export function Folded({ text, lines = 20, mono = false, fade = false, words }: FoldedProps): ReactElement {
  const { shownLines, overflow, open, toggle, label: defaultLabel } = useFolded(text, lines);
  const label = words === undefined ? defaultLabel : open ? words.open : words.closed;
  const folded = fade && overflow && !open;

  return (
    <div className="flex flex-col items-start gap-1">
      <pre
        className={cn(
          "whitespace-pre-wrap break-words",
          mono ? "font-mono text-caption" : "font-sans text-body",
          folded && FOLD_FADE_CLASS,
        )}
      >
        {shownLines.join("\n")}
      </pre>
      {overflow ? (
        <button type="button" onClick={toggle} className="text-caption text-label-3 underline">
          {label}
        </button>
      ) : null}
    </div>
  );
}
