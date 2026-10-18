"use client";

import { useMutation } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Modal, ModalDescription, ModalTitle } from "client-core/ui/modal";
import { Textarea } from "client-core/ui/textarea";
import { useRouter } from "next/navigation";
import type { ReactElement } from "react";
import { useEffect, useId, useState } from "react";

import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";

// Making the next version of a workflow out of a run that disappointed.
//
// What this screen can change by itself is what the workflow says it does. That is a real edit and
// it is the whole edit: the steps are changed in the designer, which is where the new version
// opens. What makes it an adaptation rather than a similar workflow is everything recorded beside
// it -- the parent it came from, the run being adapted from, what that run was observed to do, and
// the owner's own reason.
//
// Nothing here writes to the parent workflow or to the run it cites, and nothing here saves: the
// new version lands as a draft, and a person still presses Save on it.

export interface AdaptDialogProps {
  open: boolean;
  workflowId: string;
  /** What the workflow says it does today, which the box opens holding. */
  description: string;
  /** The ended run this version would exist because of. Empty cites none. */
  sourceRunId: string;
  /** What that run was called, for the line naming what is being adapted from. */
  sourceRunTitle: string;
  onCancel: () => void;
}

export function AdaptDialog({
  open,
  workflowId,
  description,
  sourceRunId,
  sourceRunTitle,
  onCancel,
}: AdaptDialogProps): ReactElement {
  const router = useRouter();
  const doesId = useId();
  const whyId = useId();
  const [does, setDoes] = useState(description);
  const [why, setWhy] = useState("");

  // Opened fresh every time: what was typed for an attempt somebody backed out of never carries
  // into the next one, and the box holds what the workflow says today rather than a stale edit.
  useEffect(() => {
    if (!open) return;
    setDoes(description);
    setWhy("");
  }, [open, description]);

  const adapt = useMutation({
    retry: 0,
    mutationFn: async (): Promise<string> => {
      // The session exists to hold the new version's draft, so a refused adaptation takes it back
      // out again rather than leaving an empty conversation in the list for somebody to wonder at.
      const created = await sessionsApi.create(undefined, "chat");
      try {
        await sessionsApi.adapt(created.id, {
          workflowId,
          ops: [{ op: "set_description", to: does.trim() }],
          sourceRunId,
          feedback: why.trim(),
        });
      } catch (refused) {
        await sessionsApi.remove(created.id).catch(() => undefined);
        throw refused;
      }
      return created.id;
    },
    onSuccess: (sessionId) => {
      router.push(`/workflows/draft?session=${encodeURIComponent(sessionId)}`);
    },
  });

  const unchanged = does.trim() === description.trim() || does.trim() === "";

  return (
    <Modal open={open} onClose={onCancel} className="max-w-lg">
      <div className="flex max-h-[80vh] flex-col gap-3 overflow-y-auto">
        <ModalTitle className="text-subtitle">Make a new version</ModalTitle>
        <ModalDescription className="mt-0 text-caption text-label-3">
          {sourceRunId === ""
            ? "The new version keeps everything this one does, with the description you write here."
            : `Adapting from ${sourceRunTitle || sourceRunId}. What that run produced is recorded with the new version.`}
        </ModalDescription>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={doesId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            What this version does
          </label>
          <Textarea
            id={doesId}
            value={does}
            rows={3}
            disabled={adapt.isPending}
            onChange={(event) => setDoes(event.target.value)}
          />
          <p className="text-caption text-label-3">
            The steps carry over unchanged. Open the new version in the designer to change those.
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={whyId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Why
          </label>
          <Textarea
            id={whyId}
            value={why}
            rows={2}
            disabled={adapt.isPending}
            onChange={(event) => setWhy(event.target.value)}
          />
        </div>

        {adapt.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            {studioErrorMessage(adapt.error)}
          </p>
        ) : null}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={adapt.isPending}>
            Cancel
          </Button>
          <Button onClick={() => adapt.mutate()} disabled={adapt.isPending || unchanged}>
            {adapt.isPending ? "Making it" : "Make the new version"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
