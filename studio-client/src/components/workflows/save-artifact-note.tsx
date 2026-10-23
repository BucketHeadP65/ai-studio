"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { type ReactElement, useId, useState } from "react";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import type { ArtifactRef } from "@/api/studio-types";
import type { NoteDraft, NotePreview, RetainedNote } from "@/api/types";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Modal, ModalDescription, ModalTitle } from "@/shared/ui/modal";
import { Textarea } from "@/shared/ui/textarea";

// Keeping what a run worked out, as a note in a knowledge base the person chooses.
//
// The run proposed the note. The person chooses where it goes, reads it as that destination would
// render it, changes it if they want to, and only then keeps it. Nothing here writes on its own,
// and nothing here decides where the note lands: the destination mints the id from where the value
// came from, so editing the words never moves the file.
//
// A preview is what the person is agreeing to, so Keep is only offered while the draft on screen
// is the draft that was previewed. Change a word and the preview has to be taken again. The host
// checks the same thing from its own side, by the digest the preview answered with, so this is the
// courtesy rather than the safeguard.
//
// A request id belongs to one previewed note, not to one opening. Every attempt at keeping that
// exact note carries it, so a retry after a lost answer meets its own earlier outcome rather than
// writing a second note. Take the preview again and the note is a different one, so it gets an id
// of its own and the host has no reason to refuse it.

export interface SaveArtifactNoteProps {
  /** The reference the host minted for the proposed note, exactly as this press asked about it. */
  origin: ArtifactRef;
  /** The proposed note the run produced, as the host resolved it. */
  proposed: NoteDraft;
  onClose: () => void;
}

// What the person is told once the keep has landed. Each outcome is a different thing to know, and
// only one of them leaves anything left to do.
const OUTCOME: Record<RetainedNote["outcome"], string> = {
  saved: "Kept, and indexed.",
  saved_not_indexed: "Kept, but not indexed yet. Searches will not find it until it is.",
  already_indexed: "This exact note was already kept, so nothing was written.",
};

// A draft the person has changed, compared against the one the destination last rendered. Equality
// by value, since that is what the host's own digest is over.
function sameDraft(left: NoteDraft, right: NoteDraft): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function withSectionBody(draft: NoteDraft, at: number, body: string): NoteDraft {
  return {
    ...draft,
    sections: draft.sections.map((section, index) =>
      index === at ? { ...section, body } : section,
    ),
  };
}

export function SaveArtifactNote({
  origin,
  proposed,
  onClose,
}: SaveArtifactNoteProps): ReactElement {
  const destinationId = useId();
  const titleId = useId();
  const summaryId = useId();
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [destination, setDestination] = useState("");
  const [draft, setDraft] = useState<NoteDraft>(proposed);
  const [previewed, setPreviewed] = useState<NotePreview | null>(null);

  const knowledgeBases = useQuery({
    queryKey: ["knowledge-bases", "registry"],
    queryFn: () => knowledgeBaseApi.knowledgeBases(),
  });

  const preview = useMutation({
    retry: 0,
    mutationFn: (asked: { name: string; edited: NoteDraft | undefined }): Promise<NotePreview> =>
      knowledgeBaseApi.previewNote(asked.name, origin, asked.edited),
    onSuccess: (answered) => {
      setPreviewed(answered);
      setDraft(answered.draft);
      setRequestId(crypto.randomUUID());
    },
  });

  const keep = useMutation({
    retry: 0,
    mutationFn: (agreed: NotePreview): Promise<RetainedNote> =>
      knowledgeBaseApi.retainNote(agreed.knowledge_base, {
        idempotency_key: requestId,
        origin,
        draft: agreed.draft,
        schema_digest: agreed.schema_digest,
        preview_digest: agreed.preview_digest,
      }),
  });

  const kept = keep.data ?? null;
  const stale = previewed !== null && !sameDraft(draft, previewed.draft);
  const busy = preview.isPending || keep.isPending;
  const settled = kept !== null && kept.outcome !== "saved_not_indexed";

  // Reading the note as one destination would write it. Whatever the last attempt came to goes
  // with the note it was about, so what is on screen is only ever about the note now in hand.
  function takePreview(name: string, edited: NoteDraft | undefined): void {
    keep.reset();
    preview.mutate({ name, edited });
  }

  function choose(name: string): void {
    setDestination(name);
    setPreviewed(null);
    setDraft(proposed);
    keep.reset();
    if (name !== "") takePreview(name, undefined);
  }

  return (
    <Modal
      open
      onClose={() => {
        if (!busy) onClose();
      }}
      className="max-w-2xl"
    >
      <div className="flex max-h-[80vh] flex-col gap-3 overflow-y-auto">
        <ModalTitle className="text-subtitle">Keep this as a note</ModalTitle>
        <ModalDescription className="mt-0 text-caption text-label-3">
          Choose where it goes, read it as that knowledge base would write it, and keep it when you
          are happy. Nothing is written until you do.
        </ModalDescription>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={destinationId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Knowledge base
          </label>
          <select
            id={destinationId}
            value={destination}
            disabled={busy || settled}
            onChange={(event) => choose(event.target.value)}
            className="h-9 rounded-md border border-line bg-surface px-2 text-body text-label-1"
          >
            <option value="">Choose one</option>
            {(knowledgeBases.data?.knowledge_bases ?? [])
              .filter((row) => row.registered)
              .map((row) => (
                <option key={row.name} value={row.name}>
                  {row.name}
                </option>
              ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={titleId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Title
          </label>
          <Input
            id={titleId}
            value={draft.title}
            disabled={busy || settled}
            onChange={(event) => setDraft({ ...draft, title: event.target.value })}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={summaryId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Summary
          </label>
          <Textarea
            id={summaryId}
            value={draft.summary}
            rows={2}
            disabled={busy || settled}
            onChange={(event) => setDraft({ ...draft, summary: event.target.value })}
          />
        </div>

        {draft.sections.map((section, index) => (
          <div key={`${index}-${section.heading}`} className="flex flex-col gap-1">
            <label
              htmlFor={`${summaryId}-${index}`}
              className="font-semibold text-label-3 text-micro uppercase tracking-caps"
            >
              {section.heading}
            </label>
            <Textarea
              id={`${summaryId}-${index}`}
              value={section.body}
              rows={3}
              disabled={busy || settled}
              onChange={(event) => setDraft(withSectionBody(draft, index, event.target.value))}
            />
          </div>
        ))}

        {previewed !== null ? (
          <div className="flex flex-col gap-1.5 rounded-md border border-line bg-well px-3 py-2.5">
            <p className="font-semibold text-label-3 text-micro uppercase tracking-caps">
              Where it would go
            </p>
            <p className="font-mono text-caption text-label-2">{previewed.path}</p>
            {previewed.warnings.map((warning) => (
              <p key={warning} className="text-caption text-label-3">
                {warning}
              </p>
            ))}
          </div>
        ) : null}

        {stale ? (
          <p role="status" className="text-caption text-label-3">
            You have changed the note since it was read. Take the preview again before keeping it.
          </p>
        ) : null}

        {preview.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            {knowledgeBaseErrorMessage(preview.error)}
          </p>
        ) : null}
        {keep.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            {knowledgeBaseErrorMessage(keep.error)}
          </p>
        ) : null}
        {kept !== null ? (
          <p role="status" className="text-caption text-label-2">
            {OUTCOME[kept.outcome]}
          </p>
        ) : null}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {settled ? "Close" : "Cancel"}
          </Button>
          {stale && !settled ? (
            <Button
              variant="secondary"
              onClick={() => {
                if (destination !== "") takePreview(destination, draft);
              }}
              disabled={busy}
            >
              {preview.isPending ? "Reading" : "Preview again"}
            </Button>
          ) : null}
          {!settled ? (
            <Button
              onClick={() => {
                if (previewed !== null && !stale) keep.mutate(previewed);
              }}
              disabled={busy || previewed === null || stale}
            >
              {keep.isPending
                ? "Keeping"
                : kept?.outcome === "saved_not_indexed"
                  ? "Try indexing again"
                  : "Keep"}
            </Button>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
