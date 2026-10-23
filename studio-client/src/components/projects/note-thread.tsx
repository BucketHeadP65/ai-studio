"use client";

import { useMutation } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import type { ReactElement } from "react";
import { useState } from "react";

import { type NoteListingEntry, type NoteReply, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { NodeGlyph } from "@/components/marks/node-glyph";
import { StatePill } from "@/components/marks/state-pill";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { Textarea } from "@/shared/ui/textarea";

export interface NoteThreadProps {
  projectId: string;
  path: string;
  note: NoteListingEntry;
  onChanged: () => void;
}

const FIELD_TEXTAREA_CLASSNAME =
  "resize-none border-0 bg-transparent p-0 focus:border-0 focus:outline-none focus:ring-0";

// The full discussion for one note, styled as the note card: a status row (anchor lost,
// resolved), the anchored quote, the note text, the reply thread, a reply composer, and its
// action row. Shared by the margin's hover card and the click-through-the-highlighted-span
// popover, so neither owns its own copy of this logic. The note's own text is owner-authored and
// always editable; only an owner-authored reply gets the same affordance, never an agent one. A
// resolved thread drops the reply composer and its Reply action, since resolving closes the
// conversation until it is reopened.
export function NoteThread({ projectId, path, note, onChanged }: NoteThreadProps): ReactElement {
  const [replyText, setReplyText] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [editingText, setEditingText] = useState<string | null>(null);
  const resolved = note.status === "resolved";

  const reply = useMutation({
    mutationFn: () => projectsApi.replyToNote(projectId, note.id, path, replyText),
    onSuccess: () => {
      setReplyText("");
      onChanged();
    },
  });

  const setStatus = useMutation({
    mutationFn: () =>
      resolved
        ? projectsApi.reopenNote(projectId, note.id, path)
        : projectsApi.resolveNote(projectId, note.id, path),
    onSuccess: onChanged,
  });

  const remove = useMutation({
    mutationFn: () => projectsApi.deleteNote(projectId, note.id, path),
    onSuccess: () => {
      setConfirmingDelete(false);
      onChanged();
    },
  });

  const editNote = useMutation({
    mutationFn: (text: string) => projectsApi.updateNote(projectId, note.id, { path, text }),
    onSuccess: () => {
      setEditingText(null);
      onChanged();
    },
  });

  const error = reply.error ?? setStatus.error ?? remove.error ?? editNote.error;

  function submitReply(): void {
    if (replyText.trim() === "") return;
    reply.mutate();
  }

  function saveNoteEdit(): void {
    if (editingText === null || editingText.trim() === "") return;
    editNote.mutate(editingText);
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line bg-surface px-5 py-4 shadow-sm">
      {note.orphaned || resolved ? (
        <div className="flex flex-col gap-2">
          {note.orphaned ? (
            <div className="flex flex-wrap items-center gap-2">
              <StatePill tone="warning" size="sm">
                Anchor lost
              </StatePill>
              <span className="text-caption text-label-3">
                The lines this note pointed at were rewritten.
              </span>
            </div>
          ) : null}
          {resolved ? (
            <div className="flex flex-wrap items-center gap-2">
              <StatePill tone="completed" size="sm">
                Resolved
              </StatePill>
              <span className="text-caption text-label-3">Resolved.</span>
            </div>
          ) : null}
        </div>
      ) : null}

      <blockquote
        title={note.anchor.exact}
        className="line-clamp-3 border-line border-l-2 pl-3 font-mono text-body text-label-3 leading-relaxed"
      >
        {note.anchor.exact}
      </blockquote>

      {editingText === null ? (
        <div className="flex items-start gap-2">
          <p className="min-w-0 flex-1 text-body text-label-1">{note.text}</p>
          <button
            type="button"
            aria-label="Edit note"
            onClick={() => setEditingText(note.text)}
            className="shrink-0 text-label-3 hover:text-label-1"
          >
            <Pencil size={13} strokeWidth={1.75} />
          </button>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="rounded-lg border border-line bg-surface p-2.5">
            <Textarea
              value={editingText}
              onChange={(event) => setEditingText(event.target.value)}
              rows={2}
              disabled={editNote.isPending}
              className={FIELD_TEXTAREA_CLASSNAME}
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => setEditingText(null)}
              disabled={editNote.isPending}
            >
              Cancel
            </Button>
            <Button
              onClick={saveNoteEdit}
              disabled={editNote.isPending || editingText.trim() === ""}
            >
              {editNote.isPending ? "Saving" : "Save"}
            </Button>
          </div>
        </div>
      )}

      {note.replies.length > 0 ? (
        <div className="flex flex-col gap-2.5">
          {note.replies.map((entry, index) => (
            <ReplyRow
              key={`${entry.at}-${entry.author}`}
              projectId={projectId}
              path={path}
              noteId={note.id}
              index={index}
              reply={entry}
              onChanged={onChanged}
            />
          ))}
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="text-caption text-danger-ink">
          {studioErrorMessage(error)}
        </p>
      ) : null}

      {resolved ? null : (
        <div className="rounded-md border border-line px-3.5 py-3">
          <Textarea
            value={replyText}
            onChange={(event) => setReplyText(event.target.value)}
            placeholder="Reply"
            rows={2}
            disabled={reply.isPending}
            className={FIELD_TEXTAREA_CLASSNAME}
          />
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => setStatus.mutate()}
            disabled={setStatus.isPending}
          >
            {resolved ? "Reopen" : "Resolve"}
          </Button>
          <Button
            variant="danger"
            onClick={() => setConfirmingDelete(true)}
            disabled={remove.isPending}
          >
            Delete
          </Button>
        </div>
        {resolved ? null : (
          <Button onClick={submitReply} disabled={reply.isPending || replyText.trim() === ""}>
            {reply.isPending ? "Sending" : "Reply"}
          </Button>
        )}
      </div>

      <ConfirmDialog
        open={confirmingDelete}
        title="Delete this note?"
        message="This removes the note and its replies. This cannot be undone."
        confirmLabel="Delete"
        destructive
        confirming={remove.isPending}
        onConfirm={() => remove.mutate()}
        onCancel={() => setConfirmingDelete(false)}
      />
    </div>
  );
}

interface ReplyRowProps {
  projectId: string;
  path: string;
  noteId: string;
  index: number;
  reply: NoteReply;
  onChanged: () => void;
}

// One reply in the thread, toned by author: the owner's own reply mirrors the chat station's
// user bubble (right aligned, ink), the agent's mirrors its agent bubble at card scale (a
// hexagon glyph beside a squared-corner card), and its byline says outright that it is not a
// person. Only an owner-authored reply gets an Edit affordance -- an agent reply is read only,
// matching the backend route that refuses editing it.
function ReplyRow({ projectId, path, noteId, index, reply, onChanged }: ReplyRowProps) {
  const [editingText, setEditingText] = useState<string | null>(null);

  const edit = useMutation({
    mutationFn: (text: string) =>
      projectsApi.updateReply(projectId, noteId, { path, replyIndex: index, text }),
    onSuccess: () => {
      setEditingText(null);
      onChanged();
    },
  });

  function saveEdit(): void {
    if (editingText === null || editingText.trim() === "") return;
    edit.mutate(editingText);
  }

  if (editingText !== null) {
    return (
      <div className="flex flex-col gap-2">
        <div className="rounded-lg border border-line bg-surface p-2.5">
          <Textarea
            value={editingText}
            onChange={(event) => setEditingText(event.target.value)}
            rows={2}
            disabled={edit.isPending}
            className={FIELD_TEXTAREA_CLASSNAME}
          />
        </div>
        {edit.error ? (
          <p role="alert" className="text-caption text-danger-ink">
            {studioErrorMessage(edit.error)}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => setEditingText(null)}
            disabled={edit.isPending}
          >
            Cancel
          </Button>
          <Button onClick={saveEdit} disabled={edit.isPending || editingText.trim() === ""}>
            {edit.isPending ? "Saving" : "Save"}
          </Button>
        </div>
      </div>
    );
  }

  if (reply.author === "owner") {
    return (
      <div className="flex flex-col items-end gap-1">
        <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">You</p>
        <div className="flex max-w-[85%] items-start gap-1.5 rounded-tl-lg rounded-tr-lg rounded-br-sm rounded-bl-lg bg-label-1 px-3.5 py-2.5">
          <p className="min-w-0 flex-1 text-body text-surface">{reply.text}</p>
          <button
            type="button"
            aria-label="Edit reply"
            onClick={() => setEditingText(reply.text)}
            className="shrink-0 text-surface/70 hover:text-surface"
          >
            <Pencil size={13} strokeWidth={1.75} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2">
      <NodeGlyph kind="agent-gate" fill="ran" size={22} className="mt-0.5 shrink-0" />
      <div className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-tl-sm rounded-tr-lg rounded-br-lg rounded-bl-lg border border-line bg-well px-3 py-2.5">
        <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">
          Orchestrator agent, not a person
        </p>
        <p className="text-body text-label-1">{reply.text}</p>
      </div>
    </div>
  );
}
