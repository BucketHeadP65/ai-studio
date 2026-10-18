"use client";

import type { ReactElement } from "react";

import type { RunView } from "@/api/sessions-types";
import type { WorkflowGraph } from "@/api/studio-types";
import { NodeGlyph } from "@/components/marks/node-glyph";

import { RunSummary } from "./run-summary";

// The transcript's own turn for this session's live run: the same agent-bubble grid an assistant
// message draws (the hexagon avatar column), with a "Run" byline instead of the agent's, holding
// the run's own summary instead of prose. Never clickable: there is no turn detail behind a run,
// only the run's own page, which `RunSummary` already links to.

export interface RunTurnProps {
  title: string;
  run: RunView;
  graph: WorkflowGraph | undefined;
  now: number;
}

export function RunTurn({ title, run, graph, now }: RunTurnProps): ReactElement {
  return (
    <div className="flex max-w-[92%] items-start gap-2.5 self-start">
      <NodeGlyph kind="agent-gate" fill="ran" size={30} />
      <div className="flex min-w-0 flex-1 flex-col items-start gap-1.5 rounded-tl-sm rounded-tr-lg rounded-br-lg rounded-bl-lg border border-line bg-surface px-3.5 py-3 shadow-sm">
        <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">Run</p>
        <RunSummary title={title} run={run} graph={graph} now={now} />
      </div>
    </div>
  );
}
