"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { Input } from "client-core/ui/input";
import { Textarea } from "client-core/ui/textarea";
import type { ReactElement } from "react";
import { useState } from "react";

import Link from "next/link";

import { refusedReadiness, studioApi, studioErrorMessage } from "@/api/studio";
import type { Readiness, Trust, WorkflowDetail } from "@/api/studio-types";
import { ARCHIVE_CONFIRM } from "@/components/workflows/archive-copy";
import { FieldFinding, ReadinessNotice } from "@/components/workflows/readiness-findings";
import { TrustChip } from "@/components/workflows/trust-chip";

// The identity an open workflow carries: what it is (title, id@version, trust, the words that
// describe it) and the two routes that change its card, title-only up here so its height follows
// its content rather than whatever else the page shows beside it. Edit flips the three fields
// this route owns -- title, summary, applicability -- into a form, wired to `PATCH .../card`; the
// spec itself is never touched here, adapting it is a different route. Acting on a run (the
// params, the gates on this run, Run, Schedule it) lives in its own card in the side column,
// never here.
//
// The form shows the backend's own reading of what this card still lacks: the title's and the
// summary's beside their boxes, everything else as a list under them. The author's four sentences
// are written on the "what it does" page, which has room for them, and a patch that names none of
// them leaves whatever the library holds. A refusal replaces that reading with the one the refusal
// carried, so the form always shows the answer to the request actually made.

export interface WorkflowHeaderProps {
  detail: WorkflowDetail;
}

export function WorkflowHeader({ detail }: WorkflowHeaderProps): ReactElement {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(detail.title);
  const [summary, setSummary] = useState(detail.summary);
  const [applicability, setApplicability] = useState(detail.applicability);
  const [archiveOpen, setArchiveOpen] = useState(false);

  const save = useMutation({
    mutationFn: () => studioApi.redescribeWorkflow(detail.id, { title, summary, applicability }),
    onSuccess: (card) => {
      queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
        previous ? { ...previous, ...card } : previous,
      );
      setEditing(false);
    },
  });
  const reading: Readiness | null = save.isError
    ? refusedReadiness(save.error)
    : (detail.readiness ?? null);

  function invalidateLibrary() {
    queryClient.invalidateQueries({ queryKey: ["library"] });
  }

  const archive = useMutation({
    mutationFn: () => studioApi.archiveWorkflow(detail.id),
    onSuccess: (card) => {
      queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
        previous ? { ...previous, archived_at: card.archived_at } : previous,
      );
      setArchiveOpen(false);
      invalidateLibrary();
    },
  });
  const restore = useMutation({
    mutationFn: () => studioApi.restoreWorkflow(detail.id),
    onSuccess: (card) => {
      queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
        previous ? { ...previous, archived_at: card.archived_at } : previous,
      );
      invalidateLibrary();
    },
  });

  function startEditing() {
    setTitle(detail.title);
    setSummary(detail.summary);
    setApplicability(detail.applicability);
    save.reset();
    setEditing(true);
  }

  function onApproved(trust: Trust) {
    queryClient.setQueryData<WorkflowDetail>(["studio", "workflow", detail.id], (previous) =>
      previous ? { ...previous, trust } : previous,
    );
  }

  const displayTitle = detail.title || detail.id;

  return (
    <>
      <div className="min-w-0 flex-1 space-y-1.5">
        {editing ? (
          <div className="max-w-xl space-y-2">
            <div className="flex flex-col gap-1">
              <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
                Title
              </span>
              <Input
                aria-label="Title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                className="h-8 text-body"
              />
              <FieldFinding readiness={reading} field="title" />
            </div>
            <div className="flex flex-col gap-1">
              <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
                Summary
              </span>
              <Textarea
                aria-label="Summary"
                value={summary}
                onChange={(event) => setSummary(event.target.value)}
                rows={2}
                className="text-caption"
              />
              <FieldFinding readiness={reading} field="summary" />
            </div>
            <div className="flex flex-col gap-1">
              <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
                Applicability
              </span>
              <Textarea
                aria-label="Applicability"
                value={applicability}
                onChange={(event) => setApplicability(event.target.value)}
                rows={2}
                className="text-caption"
              />
            </div>
            <ReadinessNotice readiness={reading} exclude={["title", "summary"]} />
            {save.isError ? (
              <p className="text-caption text-danger-ink">{studioErrorMessage(save.error)}</p>
            ) : null}
            <div className="flex gap-2">
              <Button
                onClick={() => save.mutate()}
                disabled={save.isPending}
                className="h-8 px-3 text-caption"
              >
                {save.isPending ? "Saving" : "Save"}
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  setEditing(false);
                  save.reset();
                }}
                className="h-8 px-3 text-caption"
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="min-w-0 truncate font-semibold text-label-1 text-title">
                {displayTitle}
              </h1>
              <span className="whitespace-nowrap rounded-full bg-well px-2 py-0.5 font-mono text-label-3 text-micro">
                {detail.id} (v{detail.version})
              </span>
              <TrustChip workflowId={detail.id} trust={detail.trust} onApproved={onApproved} />
              {detail.archived_at !== null ? (
                <>
                  <Badge tone="neutral">archived</Badge>
                  <Button
                    variant="secondary"
                    onClick={() => restore.mutate()}
                    disabled={restore.isPending}
                    className="h-7 px-2.5 text-caption"
                  >
                    {restore.isPending ? "Restoring" : "Restore"}
                  </Button>
                </>
              ) : (
                <Button
                  variant="secondary"
                  onClick={() => setArchiveOpen(true)}
                  className="h-7 px-2.5 text-caption"
                >
                  Archive
                </Button>
              )}
              <Button
                variant="secondary"
                onClick={startEditing}
                className="h-7 px-2.5 text-caption"
              >
                Edit
              </Button>
              <Link
                href={`/workflows/library/${encodeURIComponent(detail.id)}`}
                className="inline-flex h-7 items-center rounded-full border border-line-strong bg-surface px-2.5 text-caption text-label-2 hover:bg-well"
              >
                What it does
              </Link>
            </div>
            <p className="text-body text-label-2">{detail.summary || "No summary yet."}</p>
            {detail.applicability ? (
              <p title={detail.applicability} className="line-clamp-1 text-caption text-label-3">
                {detail.applicability}
              </p>
            ) : null}
          </>
        )}
      </div>
      <ConfirmDialog
        open={archiveOpen}
        title={ARCHIVE_CONFIRM.title}
        message={ARCHIVE_CONFIRM.message}
        confirmLabel={ARCHIVE_CONFIRM.confirmLabel}
        confirming={archive.isPending}
        onConfirm={() => archive.mutate()}
        onCancel={() => setArchiveOpen(false)}
      />
    </>
  );
}
