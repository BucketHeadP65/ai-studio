"use client";

import { useMutation } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { type ReactElement, useId, useState } from "react";

import { studioApi, studioErrorMessage } from "@/api/studio";
import type { FlagKind, MaintenanceFlag } from "@/api/studio-types";
import { cn } from "@/shared/lib/cn";
import { Badge } from "@/shared/ui/badge";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Modal, ModalDescription, ModalTitle } from "@/shared/ui/modal";
import { Textarea } from "@/shared/ui/textarea";

// One maintenance flag in a list: what it asks the owner to decide, the step it is about and the
// workflow holding that step's copy, the evidence and any proposed change folded away until asked
// for, and the one act on it, resolving it with a reason.
//
// Whoever resolves types their own name, as approving a workflow does. This deployment has no
// accounts and no logins, so the name is text, recorded as given and never filled in on anybody's
// behalf.

// The kind spelled out for reading, never the wire value.
const FLAG_KIND_LABEL: Record<FlagKind, string> = {
  diverged_copy: "Diverged copy",
  consumer_flag: "Flagged consumer",
  live_trial_reverted: "Live trial reverted",
  plan_rejected: "Plan rejected",
  repository_change: "Repository change",
};

export interface FlagCardProps {
  flag: MaintenanceFlag;
  /** Called once the harness has resolved the flag, so the list can read the open flags again. */
  onResolved: () => void;
}

// One folded section of the flag, its text as written: the evidence carries a fenced diff, and
// reading it raw keeps every line of that diff where its author put it.
function Fold({ label, text }: { label: string; text: string }): ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-ground border-t">
      <button
        type="button"
        onClick={() => setOpen((shown) => !shown)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2 text-left text-caption text-label-3"
      >
        <ChevronDown
          aria-hidden
          size={14}
          className={cn("shrink-0 transition-transform", !open && "-rotate-90")}
        />
        {label}
      </button>
      {open ? (
        <pre className="mx-4 mb-3 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md bg-well px-3 py-2 font-mono text-label-2 text-micro">
          {text}
        </pre>
      ) : null}
    </div>
  );
}

// The modal that resolves one flag. Mounted only while open, so each opening starts empty. Esc,
// the backdrop and Cancel close it, except while the answer is on its way.
function ResolveDialog({
  flag,
  onClose,
  onResolved,
}: {
  flag: MaintenanceFlag;
  onClose: () => void;
  onResolved: () => void;
}): ReactElement {
  const nameId = useId();
  const reasonId = useId();
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");

  const resolve = useMutation({
    mutationFn: () => studioApi.resolveFlag(flag.id, name.trim(), reason.trim()),
    onSuccess: onResolved,
  });

  const ready = name.trim() !== "" && reason.trim() !== "";

  return (
    <Modal
      open
      onClose={() => {
        if (!resolve.isPending) onClose();
      }}
      className="max-w-lg"
    >
      <div className="flex flex-col gap-3">
        <ModalTitle className="text-subtitle">Resolve this flag</ModalTitle>
        <ModalDescription className="mt-0 text-caption text-label-3">
          Your name and your reason go on record with the flag, and it leaves this list.
        </ModalDescription>
        <p className="rounded-md border border-line bg-well px-3 py-2.5 text-caption text-label-1">
          {flag.summary}
        </p>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={nameId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Your name
          </label>
          <Input
            id={nameId}
            value={name}
            disabled={resolve.isPending}
            onChange={(event) => setName(event.target.value)}
          />
          <p className="text-caption text-label-3">
            Recorded as who resolved it. There are no accounts here, so nothing fills this in for
            you.
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={reasonId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Why
          </label>
          <Textarea
            id={reasonId}
            value={reason}
            rows={3}
            disabled={resolve.isPending}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>

        {resolve.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            {studioErrorMessage(resolve.error)}
          </p>
        ) : null}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={resolve.isPending}>
            Cancel
          </Button>
          <Button onClick={() => resolve.mutate()} disabled={resolve.isPending || !ready}>
            {resolve.isPending ? "Resolving" : "Resolve"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function FlagCard({ flag, onResolved }: FlagCardProps): ReactElement {
  const [resolving, setResolving] = useState(false);

  return (
    <li className="overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
      <div className="flex flex-col gap-2 px-4 pt-2.5 pb-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <Badge tone="warn">{FLAG_KIND_LABEL[flag.kind]}</Badge>
          <span className="text-caption text-label-3">
            Step <span className="font-mono text-label-1">{flag.step}</span>
          </span>
          {/* A flag about a step has no workflow, and says nothing rather than an empty one. */}
          {flag.workflow !== "" ? (
            <span className="min-w-0 truncate text-caption text-label-3">
              Workflow <span className="font-mono text-label-1">{flag.workflow}</span>
            </span>
          ) : null}
        </div>
        <p className="text-body text-label-1">{flag.summary}</p>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-ground border-t px-4 py-2 sm:justify-end">
        <Button onClick={() => setResolving(true)} className="h-8 px-4 text-caption">
          Resolve
        </Button>
      </div>

      <Fold label="Evidence" text={flag.evidence} />
      {flag.proposed_change !== "" ? (
        <Fold label="Proposed change" text={flag.proposed_change} />
      ) : null}

      {resolving ? (
        <ResolveDialog
          flag={flag}
          onClose={() => setResolving(false)}
          onResolved={() => {
            setResolving(false);
            onResolved();
          }}
        />
      ) : null}
    </li>
  );
}
