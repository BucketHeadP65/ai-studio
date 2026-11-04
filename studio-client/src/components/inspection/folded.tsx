"use client";

import { type ReactElement, useState } from "react";

// A block of text that folds past a line count, once, no animation (the owner's Reduce Motion
// is always on). Folded, past its line count, it is cut at a whole line, never by an ellipsis
// character. `FOLD_FADE_CLASS` lightens a cut with a bottom mask instead of a hard edge.

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
}

export function Folded({ text, lines = 20 }: FoldedProps): ReactElement {
  const { shownLines, overflow, toggle, label } = useFolded(text, lines);

  return (
    <div className="flex flex-col items-start gap-1">
      <pre className="whitespace-pre-wrap break-words font-sans text-body">{shownLines.join("\n")}</pre>
      {overflow ? (
        <button type="button" onClick={toggle} className="text-caption text-label-3 underline">
          {label}
        </button>
      ) : null}
    </div>
  );
}
