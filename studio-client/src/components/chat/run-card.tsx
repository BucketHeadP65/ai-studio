"use client";

import type { ReactElement } from "react";

import { ProvenanceChip, provenance } from "@/components/marks/provenance-chip";
import { RUN_STATE_PILL } from "@/components/marks/run-state";
import { StatePill } from "@/components/marks/state-pill";

// One live run worth watching: which session it belongs to, what workflow it
// is running, how far it has gotten, or what it is waiting on. Many of these
// can be on screen at once, one per session with a run in flight.

export type RunCardStatus = "running" | "waiting" | "paused";

export interface RunCardProps {
  sessionTitle: string;
  workflowId: string;
  status: RunCardStatus;
  done: number;
  active: number;
  gateId: string | null;
}

export function RunCard({
  sessionTitle,
  workflowId,
  status,
  done,
  active,
  gateId,
}: RunCardProps): ReactElement {
  return (
    <div className="rounded-lg border border-line bg-surface p-3 shadow-sm transition-colors hover:border-line-strong hover:bg-well">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-body font-semibold text-label-1">
          {sessionTitle}
        </span>
        <StatePill tone={RUN_STATE_PILL[status].tone} size="sm">
          {RUN_STATE_PILL[status].word}
        </StatePill>
      </div>
      <p className="mt-1 truncate font-mono text-label-3 text-micro">{workflowId}</p>
      <div className="mt-1.5 space-y-1.5">
        {status === "running" ? (
          <p className="text-label-2 text-caption">
            <span className="font-mono">{done}</span> done,{" "}
            <span className="font-mono">{active}</span> active
          </p>
        ) : null}
        {status === "waiting" ? (
          <>
            <p className="text-label-2 text-caption">
              waiting at{" "}
              {gateId !== null ? <span className="font-mono">{gateId}</span> : "the gate"}
            </p>
            <ProvenanceChip actor="person-pending" variant="inline">
              {provenance.needsAPerson()}
            </ProvenanceChip>
          </>
        ) : null}
      </div>
    </div>
  );
}
