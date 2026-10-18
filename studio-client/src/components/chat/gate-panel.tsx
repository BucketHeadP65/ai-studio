"use client";

import { cn } from "client-core/lib/cn";
import { Button } from "client-core/ui/button";
import { Textarea } from "client-core/ui/textarea";
import type { ReactElement } from "react";
import { useEffect, useRef, useState } from "react";

import type { PendingGate } from "@/api/studio-types";
import { ProvenanceChip, provenance } from "@/components/marks/provenance-chip";
import { StatePill } from "@/components/marks/state-pill";

// The one gate a person answers: which workflow is waiting, on which gate id,
// and the value under review exactly as the backend rendered it. Approve
// stays one click. Reject stays one click too, with no feedback. Reject with
// feedback reveals a textarea, and only that path carries text on the
// rejection it posts.
//
// `chrome` picks how much of the panel draws its own frame. "card" (the
// default) is the standalone panel: its own border, a header naming the
// workflow and gate, and the payload block below it. "bare" drops the
// border and the header for a caller that already supplies both around this
// panel's body (the gate turn in the transcript) — the rejection-reruns fact
// the header would otherwise state moves to a caption line under the
// buttons instead, so it stays readable either way.

export type GatePanelChrome = "card" | "bare";

export interface GatePanelProps {
  pending: PendingGate;
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
  chrome?: GatePanelChrome;
}

export function GatePanel({
  pending,
  onDecide,
  deciding,
  chrome = "card",
}: GatePanelProps): ReactElement {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedback, setFeedback] = useState("");
  const feedbackRef = useRef<HTMLDivElement | null>(null);
  // The field opens at the bottom of a scrolling transcript, so bring it into view on open.
  useEffect(() => {
    if (feedbackOpen) feedbackRef.current?.scrollIntoView({ block: "nearest" });
  }, [feedbackOpen]);

  function sendRejection() {
    onDecide(false, feedback);
    setFeedback("");
    setFeedbackOpen(false);
  }

  const body = (
    <div className={chrome === "card" ? "space-y-3 bg-surface px-4 py-3.5" : "space-y-3"}>
      <p
        className={cn(
          "max-w-prose whitespace-pre-wrap rounded-md border border-line bg-well px-3.5 py-3 text-body text-label-1",
          // A card keeps the reviewed text in its own scroll box so a stack of cards stays even;
          // inside a thread the text flows at full height, so the thread is the one scroller.
          chrome === "card" && "max-h-48 overflow-auto",
        )}
      >
        {pending.reviewing}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={() => onDecide(true)}
          disabled={deciding}
          className="h-8 px-4 text-caption"
        >
          Approve
        </Button>
        <Button
          variant="secondary"
          onClick={() => onDecide(false)}
          disabled={deciding}
          className="h-8 px-4 text-caption"
        >
          Reject
        </Button>
        <Button
          variant="secondary"
          onClick={() => setFeedbackOpen((open) => !open)}
          disabled={deciding}
          className="h-8 px-4 text-caption"
        >
          Reject with feedback
        </Button>
      </div>
      {feedbackOpen ? (
        <div ref={feedbackRef} className="space-y-2">
          <Textarea
            aria-label="Feedback"
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            rows={3}
            placeholder="What should change"
            className="text-body"
            autoFocus
          />
          <Button
            variant="secondary"
            onClick={sendRejection}
            disabled={deciding}
            className="h-8 px-4 text-caption"
          >
            Send rejection
          </Button>
        </div>
      ) : null}
      {chrome === "bare" && pending.rejection_reruns ? (
        <p className="text-caption text-label-3">
          (a rejection reruns from <span className="font-mono">{pending.rejection_reruns}</span>)
        </p>
      ) : null}
      <ProvenanceChip actor="person-pending" variant="inline">
        {provenance.needsAPerson()}
      </ProvenanceChip>
    </div>
  );

  if (chrome === "bare") {
    return body;
  }

  return (
    <div className="overflow-hidden rounded-lg border border-warn-ink shadow-sm">
      <div className="flex flex-wrap items-center gap-2.5 border-warn-ink/20 border-b bg-warn-soft px-4 py-3">
        <StatePill tone="waiting" size="sm">
          Waiting on you
        </StatePill>
        <p className="text-caption text-label-1">
          <span className="font-medium">{pending.workflow}</span> is waiting for you at{" "}
          <span className="font-mono">{pending.gate_id}</span>
          {pending.rejection_reruns ? (
            <span className="text-label-2">
              {" "}
              (a rejection reruns from <span className="font-mono">{pending.rejection_reruns}</span>
              )
            </span>
          ) : null}
        </p>
      </div>
      {body}
    </div>
  );
}
