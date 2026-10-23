"use client";

import type { ReactElement } from "react";
import { useState } from "react";

import { Button } from "@/shared/ui/button";
import { Textarea } from "@/shared/ui/textarea";

import type { PendingApproval } from "./tool-parts";

// Takes over the composer's own place the moment the companion has an act waiting on the
// person: what it wants to do, against what, block by block, so approving takes reading the
// payload first rather than a reflex click where Send usually sits. One shared reason box
// answers whichever block's Deny sends it, and clears once it has. Reply instead hands the
// composer back without deciding anything -- typing there denies every pending act itself.

export interface ApprovalCardProps {
  pending: PendingApproval[];
  onRespond: (approvalId: string, approved: boolean, reason?: string) => void;
  onReplyInstead: () => void;
  busy: boolean;
}

export function ApprovalCard({
  pending,
  onRespond,
  onReplyInstead,
  busy,
}: ApprovalCardProps): ReactElement {
  const [reason, setReason] = useState("");
  // Answered the instant a click fires, before any round trip. A block whose act is already
  // answered leaves the list on the next render, and the block below it takes over the place
  // the person's cursor is already on, so nothing but this keeps a second click off an act
  // they never read.
  const [answered, setAnswered] = useState<ReadonlySet<string>>(new Set());
  const [handedBack, setHandedBack] = useState(false);

  function answer(approvalId: string, approved: boolean, why?: string) {
    if (answered.has(approvalId)) return;
    setAnswered((ids) => new Set(ids).add(approvalId));
    onRespond(approvalId, approved, why);
  }

  function approve(approvalId: string) {
    answer(approvalId, true, undefined);
  }

  function deny(approvalId: string) {
    answer(approvalId, false, reason === "" ? undefined : reason);
    setReason("");
  }

  function replyInstead() {
    if (handedBack) return;
    setHandedBack(true);
    onReplyInstead();
  }

  return (
    <div className="border-line border-t bg-ground px-4 py-3">
      <div className="space-y-3 rounded-xl border border-line bg-well p-3.5 shadow-sm">
        <p className="text-body font-semibold text-label-1">The companion wants to act</p>
        {pending.map((act) => (
          <div
            key={act.approvalId}
            className="space-y-2 rounded-md border border-line bg-surface p-3"
          >
            <p className="font-mono text-caption text-label-1">{act.toolName}</p>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-well px-3 py-2 font-mono text-caption text-label-2">
              {JSON.stringify(act.input, null, 2)}
            </pre>
            <div className="flex gap-2">
              <Button
                onClick={() => approve(act.approvalId)}
                disabled={busy || answered.has(act.approvalId)}
                aria-label={`Approve ${act.toolName}`}
                className="h-8 px-4 text-caption"
              >
                Approve
              </Button>
              <Button
                variant="secondary"
                onClick={() => deny(act.approvalId)}
                disabled={busy || answered.has(act.approvalId)}
                aria-label={`Deny ${act.toolName}`}
                className="h-8 px-4 text-caption"
              >
                Deny
              </Button>
            </div>
          </div>
        ))}
        <Textarea
          aria-label="Reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          rows={2}
          placeholder="Why not (optional)"
          className="text-body"
        />
        <div className="border-line border-t pt-3">
          <Button
            variant="secondary"
            onClick={replyInstead}
            disabled={busy || handedBack}
            className="h-8 px-4 text-caption"
          >
            Reply instead
          </Button>
        </div>
      </div>
    </div>
  );
}
