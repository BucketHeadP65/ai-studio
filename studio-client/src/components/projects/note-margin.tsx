"use client";

import { cn } from "client-core/lib/cn";
import { DismissableLayer } from "radix-ui/internal";
import { useEffect, useState } from "react";

import type { NoteListingEntry } from "@/api/projects";

import { NoteThread } from "./note-thread";

const LINE_SLOT_PX = 24;
/** Vertical stagger between dots that would otherwise land on the exact same top. */
const STACK_OFFSET_PX = 7;

export interface NoteMarginProps {
  projectId: string;
  path: string;
  notes: NoteListingEntry[];
  onChanged: () => void;
  /**
   * Pixel offset, within this column, of the rendered line a note is anchored to.
   * `null` means the caller could not measure it, and the dot falls back to a fixed
   * slot per line.
   */
  topFor?: (line: number) => number | null;
  /**
   * A note id to open right now, from outside this margin -- e.g. a click on the
   * matching highlighted span in the reading view. Consumed once via `onRequestOpenHandled`.
   */
  requestOpen?: string | null;
  onRequestOpenHandled?: () => void;
}

interface PositionedNote {
  note: NoteListingEntry;
  top: number;
  /** This note's place among others sharing the same top, 0 for the first. Disambiguates the label. */
  stackIndex: number;
}

// Two notes can measure to the very same top -- the same line, or two blocks close enough
// that the caller's own measurement lands on the same pixel. Left as-is, their dots would sit
// exactly on top of each other and only the last-rendered one could ever be clicked. Each note
// past the first at a given top gets nudged down by a few pixels, so every dot stays its own
// clickable target.
function stackedPositions(
  notes: NoteListingEntry[],
  topFor: ((line: number) => number | null) | undefined,
): PositionedNote[] {
  const stackIndexByTop = new Map<number, number>();
  const positioned: PositionedNote[] = [];
  for (const note of notes) {
    // A note whose anchor no longer resolves has no line to sit beside, so it keeps out of
    // the gutter; the Notes panel still lists it, flagged.
    if (note.line === null) continue;
    const baseTop = topFor?.(note.line) ?? note.line * LINE_SLOT_PX;
    const stackIndex = stackIndexByTop.get(baseTop) ?? 0;
    stackIndexByTop.set(baseTop, stackIndex + 1);
    positioned.push({ note, top: baseTop + stackIndex * STACK_OFFSET_PX, stackIndex });
  }
  return positioned;
}

// The open file's notes as a gutter of dots, one per unarchived note, each beside the
// line it annotates. A resolved thread's dot renders dimmed, so a resolved conversation
// reads as settled at a glance, before it's even opened. Hovering or clicking a dot opens
// its full thread -- the original comment, replies, a reply composer, and resolve/reopen
// and delete actions; clicking outside the card closes it. At most one card is open at a
// time -- opening one closes whichever other one was open. `requestOpen` lets a sibling
// (the highlighted span over the note's own text) open the same thread from outside.
export function NoteMargin({
  projectId,
  path,
  notes,
  onChanged,
  topFor,
  requestOpen,
  onRequestOpenHandled,
}: NoteMarginProps) {
  const [openNoteId, setOpenNoteId] = useState<string | null>(null);
  const visible = notes.filter((note) => !note.archived);
  const positioned = stackedPositions(visible, topFor);

  useEffect(() => {
    if (requestOpen === undefined || requestOpen === null) return;
    setOpenNoteId(requestOpen);
    onRequestOpenHandled?.();
  }, [requestOpen, onRequestOpenHandled]);

  return (
    <div role="group" aria-label="Notes" className="relative h-full">
      {positioned.map(({ note, top, stackIndex }) => (
        <NoteDot
          key={note.id}
          projectId={projectId}
          path={path}
          note={note}
          onChanged={onChanged}
          top={top}
          stackIndex={stackIndex}
          open={note.id === openNoteId}
          onOpenChange={setOpenNoteId}
        />
      ))}
    </div>
  );
}

interface NoteDotProps {
  projectId: string;
  path: string;
  note: NoteListingEntry;
  onChanged: () => void;
  top: number;
  /** This note's place among others sharing the same top, 0 for the first. */
  stackIndex: number;
  open: boolean;
  onOpenChange: (noteId: string | null) => void;
}

function NoteDot({
  projectId,
  path,
  note,
  onChanged,
  top,
  stackIndex,
  open,
  onOpenChange,
}: NoteDotProps) {
  return (
    <div className="absolute left-0" style={{ top }}>
      <DismissableLayer.Branch asChild>
        <button
          type="button"
          aria-label={
            stackIndex === 0
              ? `Note on line ${note.line}`
              : `Note on line ${note.line} (${stackIndex + 1})`
          }
          aria-expanded={open}
          onMouseEnter={() => onOpenChange(note.id)}
          onClick={() => onOpenChange(open ? null : note.id)}
          className={cn(
            "h-2 w-2 rounded-full",
            note.status === "resolved" ? "bg-muted-soft opacity-70" : "bg-accent",
          )}
        />
      </DismissableLayer.Branch>

      {/* The dot button above is a dismiss branch, so its own click never counts as
          outside; a click inside the thread's own confirm dialog (a nested, portaled
          Radix layer) is recognized as inside too, so confirming a delete never races
          this card closing out from under it. */}
      {open ? (
        <DismissableLayer.Root
          role="note"
          className="absolute top-0 left-4 z-40 w-96"
          onPointerDownOutside={() => onOpenChange(null)}
          onEscapeKeyDown={() => onOpenChange(null)}
        >
          <NoteThread projectId={projectId} path={path} note={note} onChanged={onChanged} />
        </DismissableLayer.Root>
      ) : null}
    </div>
  );
}
