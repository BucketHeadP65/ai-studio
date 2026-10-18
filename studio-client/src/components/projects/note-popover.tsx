"use client";

import { useMutation } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Textarea } from "client-core/ui/textarea";
import { MessageSquarePlus } from "lucide-react";
import { DismissableLayer } from "radix-ui/internal";
import { useState } from "react";

import { type NoteAnchor, type NoteListingEntry, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";

import { NoteThread } from "./note-thread";

const ANCHOR_CONTEXT_CHARS = 32;

/**
 * Builds a note anchor from the SOURCE markdown, never the rendered DOM.
 * `exact` is `source.slice(start, end)`; `prefix`/`suffix` are up to 32 source
 * characters immediately before/after it, fewer at the file's edges.
 */
export function anchorFor(source: string, start: number, end: number): NoteAnchor {
  return {
    exact: source.slice(start, end),
    prefix: source.slice(Math.max(0, start - ANCHOR_CONTEXT_CHARS), start),
    suffix: source.slice(end, end + ANCHOR_CONTEXT_CHARS),
  };
}

export interface NotePopoverPosition {
  top: number;
  left: number;
}

export interface AddNoteButtonProps {
  position: NotePopoverPosition;
  onClick: () => void;
}

// The Medium/Confluence affordance: appears near the end of a text selection, one click
// opens the composer right there. `onMouseDown` calls `preventDefault` so pressing the
// button never collapses the selection it's anchored to before `onClick` fires.
export function AddNoteButton({ position, onClick }: AddNoteButtonProps) {
  return (
    <button
      type="button"
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      className="fixed z-50 mt-1 flex items-center gap-1 rounded-md border border-line bg-surface px-2 py-1 text-caption text-label-1 shadow-md hover:bg-well"
      style={{ top: position.top, left: position.left }}
    >
      <MessageSquarePlus size={14} strokeWidth={1.75} />
      Add note
    </button>
  );
}

export interface NotePopoverProps {
  projectId: string;
  path: string;
  source: string;
  start: number;
  end: number;
  position: NotePopoverPosition;
  onSaved: () => void;
  onCancel: () => void;
}

// A textarea anchored at a text selection: Save posts the note built from the
// selection's source span (never the rendered DOM), Cancel discards it. The
// host owns re-fetching the note listing after a successful save.
export function NotePopover({
  projectId,
  path,
  source,
  start,
  end,
  position,
  onSaved,
  onCancel,
}: NotePopoverProps) {
  const [text, setText] = useState("");

  const create = useMutation({
    mutationFn: () => projectsApi.createNote(projectId, path, anchorFor(source, start, end), text),
    onSuccess: onSaved,
  });

  function submit() {
    if (text.trim() === "") return;
    create.mutate();
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: a dismissable layer anchored beside a text selection, not a modal <dialog>.
    <DismissableLayer.Root
      role="dialog"
      aria-label="New note"
      onEscapeKeyDown={onCancel}
      className="fixed z-50 w-72 rounded-xl border border-line bg-surface p-3 shadow-lg"
      style={{ top: position.top, left: position.left }}
    >
      <Textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Add a note…"
        rows={3}
        autoFocus
        disabled={create.isPending}
      />

      {create.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(create.error)}
        </p>
      ) : null}

      <div className="mt-2 flex justify-end gap-2">
        <Button variant="secondary" onClick={onCancel} disabled={create.isPending}>
          Cancel
        </Button>
        <Button onClick={submit} disabled={create.isPending || text.trim() === ""}>
          {create.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </DismissableLayer.Root>
  );
}

export interface ThreadPopoverProps {
  projectId: string;
  path: string;
  note: NoteListingEntry;
  position: NotePopoverPosition;
  onChanged: () => void;
  onClose: () => void;
}

// Opens an existing note's full thread right where the owner clicked its anchored span --
// the same fixed-position card NotePopover uses for a new note, so the conversation opens
// at the text itself, not only at the distant margin dot (which keeps working on its own).
// Closes on Escape and on an outside click, same as the margin's own hover card -- and a
// click inside its own thread's confirm dialog (a nested, portaled Radix layer) is never
// mistaken for one, so confirming a delete never races this card closing out from under it.
export function ThreadPopover({
  projectId,
  path,
  note,
  position,
  onChanged,
  onClose,
}: ThreadPopoverProps) {
  return (
    <DismissableLayer.Root
      role="note"
      onPointerDownOutside={onClose}
      onEscapeKeyDown={onClose}
      className="fixed z-50 w-96"
      style={{ top: position.top, left: position.left }}
    >
      <NoteThread projectId={projectId} path={path} note={note} onChanged={onChanged} />
    </DismissableLayer.Root>
  );
}
