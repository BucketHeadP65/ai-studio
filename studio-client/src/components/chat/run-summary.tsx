"use client";

import type { ReactElement } from "react";

import type { RunView } from "@/api/sessions-types";
import type { WorkflowGraph } from "@/api/studio-types";
import { RUN_STATE_PILL } from "@/components/marks/run-state";
import { StatePill } from "@/components/marks/state-pill";
import { formatElapsedMs } from "@/components/workflows/elapsed";
import { MiniGraph } from "@/components/workflows/mini-graph";
import { OpenRunLink } from "@/components/workflows/open-run-link";
import { type FleetCard, fillOf } from "@/components/workflows/run-card";

// The body of a session's own live run, the run turn in the transcript: the run's own name and
// workflow tag, its state, the elapsed time, the mini graph with the fleet's own fill rules, and
// the link to the run's own page. Pause, resume, and stop stay with whichever caller renders the
// controls; this component only ever reads the run.

export interface RunSummaryProps {
  title: string;
  run: RunView;
  graph: WorkflowGraph | undefined;
  now: number;
}

export function RunSummary({ title, run, graph, now }: RunSummaryProps): ReactElement {
  const elapsed =
    run.started_at !== "" ? formatElapsedMs(now - new Date(run.started_at).getTime()) : "";
  // Only `run` matters to `fillOf` for a live card; the exact kind tag it carries beyond that is
  // never read.
  const fillCard: FleetCard = { kind: "running", run, name: run.workflow_id, graph, now };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-semibold text-label-1 text-subtitle">
          {title}
        </span>
        <span className="font-mono text-label-3 text-micro">{run.workflow_id}</span>
      </div>
      <div className="flex items-center gap-2.5">
        <StatePill tone={RUN_STATE_PILL[run.state].tone} size="sm">
          {RUN_STATE_PILL[run.state].word}
        </StatePill>
        {elapsed !== "" ? (
          <span className="font-mono text-caption text-label-3">{elapsed}</span>
        ) : null}
      </div>
      <MiniGraph graph={graph} fillFor={(node) => fillOf(fillCard, node.id)} />
      <OpenRunLink href={`/workflows/${encodeURIComponent(run.workflow_id)}`} size="md" />
    </div>
  );
}
