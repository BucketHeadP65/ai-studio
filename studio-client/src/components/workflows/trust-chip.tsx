"use client";

import { useMutation } from "@tanstack/react-query";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { Input } from "client-core/ui/input";
import type { ReactElement } from "react";
import { useState } from "react";

import { studioApi, studioErrorMessage } from "@/api/studio";
import type { Approved, Trust } from "@/api/studio-types";

// The level a saved artifact has earned, and the one act that raises it: a person's own
// approval. Shared by the library card and the workflow header, so the shape a chooser reads
// and the shape an open artifact reads are never two implementations of the same three lines.
//
// Whoever approves types their own name. This deployment has no accounts and no logins, so the
// name is text, recorded as given and never filled in on anybody's behalf.

const TRUST_TONE: Record<string, "neutral" | "ok" | "accent"> = {
  unverified: "neutral",
  "machine-confirmed": "ok",
  approved: "accent",
};

// The level word, spelled out for reading rather than for a wire enum -- meaning must never
// live in the badge's color alone. Exported so any other place that reads a level off the wire
// shows the same word.
export const TRUST_LABEL: Record<string, string> = {
  unverified: "not verified",
  "machine-confirmed": "confirmed by runs",
  approved: "approved",
};

export interface TrustChipProps {
  workflowId: string;
  trust: Trust;
  onApproved: (trust: Trust) => void;
}

export function TrustChip({ workflowId, trust, onApproved }: TrustChipProps): ReactElement {
  const [approveOpen, setApproveOpen] = useState(false);
  const [approveName, setApproveName] = useState("");
  const [approveNote, setApproveNote] = useState("");

  const approve = useMutation({
    mutationFn: () => studioApi.approve(workflowId, approveName.trim(), approveNote),
    onSuccess: (result: Approved) => {
      onApproved(result.trust);
      setApproveOpen(false);
      setApproveNote("");
    },
  });

  const level = trust.level;
  const named = approveName.trim() !== "";

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge tone={TRUST_TONE[level] ?? "neutral"}>{TRUST_LABEL[level] ?? level}</Badge>
      {level === "machine-confirmed" ? (
        <span className="text-caption text-label-3">
          <span className="font-mono">{trust.runs_with_evidence}</span> runs
        </span>
      ) : null}
      {level === "approved" && trust.approved_by.length > 0 ? (
        <span className="text-caption text-label-2">by {trust.approved_by.join(", ")}</span>
      ) : null}
      {trust.failed_runs > 0 ? (
        <Badge tone="danger">
          failed <span className="font-mono">{trust.failed_runs}</span>
        </Badge>
      ) : null}
      {level !== "approved" ? (
        <Button
          variant="secondary"
          onClick={() => setApproveOpen(true)}
          className="h-7 px-2.5 text-caption"
        >
          Approve it
        </Button>
      ) : null}
      {approveOpen ? (
        <div className="flex w-full flex-col gap-1.5 rounded-lg border border-line bg-well p-3">
          <p className="text-caption text-label-2">
            Your name goes on record as approving this workflow. There are no accounts here, so
            nothing fills it in for you.
          </p>
          <Input
            aria-label="Your name"
            value={approveName}
            onChange={(event) => setApproveName(event.target.value)}
            className="h-7 text-caption"
          />
          <Input
            aria-label="Note (optional)"
            placeholder="Optional note"
            value={approveNote}
            onChange={(event) => setApproveNote(event.target.value)}
            className="h-7 text-caption"
          />
          {approve.isError ? (
            <p className="text-caption text-danger-ink">{studioErrorMessage(approve.error)}</p>
          ) : null}
          <div className="flex gap-1.5">
            <Button
              onClick={() => approve.mutate()}
              disabled={approve.isPending || !named}
              className="h-7 px-2.5 text-caption"
            >
              {approve.isPending ? "Recording" : "Approve"}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setApproveOpen(false);
                approve.reset();
              }}
              className="h-7 px-2.5 text-caption"
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
