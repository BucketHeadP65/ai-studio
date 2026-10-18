"use client";

import { cn } from "client-core/lib/cn";
import { Button } from "client-core/ui/button";
import { ChevronDown, Star } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactElement, useState } from "react";

import type { RunView } from "@/api/sessions-types";
import type { RunEnvelope, WorkflowGraph } from "@/api/studio-types";
import { GatePanel } from "@/components/chat/gate-panel";
import type { GlyphFill } from "@/components/marks/node-glyph";
import { ProvenanceChip, provenance } from "@/components/marks/provenance-chip";
import { RUN_STATE_PILL, type RunStatePill } from "@/components/marks/run-state";
import { StatePill } from "@/components/marks/state-pill";
import { formatElapsedMs } from "@/components/workflows/elapsed";
import { MiniGraph } from "@/components/workflows/mini-graph";
import { ResultSummary } from "@/components/workflows/outcome-strip";
import { runnerActor, startedBySentence } from "@/components/workflows/runner-label";
import { spendText } from "@/lib/cost-text";
import { artifactHref, runHref } from "@/lib/workflow-href";

// One card on a board, one shape for every run in view, read top to bottom in the order a person
// needs it: what the run is and what state it is in, then what it wants from a person (a waiting
// gate, or how a machine decided instead), then what it actually did and what it left behind, then
// the one act available on it, then how it ran (elapsed, cost, who started it), and last its own
// graph, folded away until somebody asks for the shape. That order is the same on a narrow screen
// as on a wide one, so the act never falls below the ids and the numbers. Deciding a waiting gate
// happens inside the gate panel itself, never here. The whole card opens the run's own page; a
// click on any of its own controls, or on the run name's own link, keeps its own meaning instead.
//
// Only an archived run carries a checked result: a run still in flight has not ended, so there is
// nothing about it to check yet and the card says nothing rather than guessing.

export type FleetCard =
  | {
      kind: "waiting";
      run: RunView;
      name: string;
      graph: WorkflowGraph | undefined;
      now: number;
      onDecide: (approve: boolean, feedback?: string) => void;
      deciding: boolean;
    }
  | { kind: "running"; run: RunView; name: string; graph: WorkflowGraph | undefined; now: number }
  | {
      kind: "paused";
      run: RunView;
      name: string;
      graph: WorkflowGraph | undefined;
      now: number;
      onResume: () => void;
      resuming: boolean;
    }
  | {
      kind: "recent";
      run: RunEnvelope;
      name: string;
      graph: WorkflowGraph | undefined;
      onTogglePin: () => void;
      pinning: boolean;
    };

const OUTCOME_PILL: Record<RunEnvelope["outcome"], RunStatePill> = {
  completed: RUN_STATE_PILL.completed,
  failed: RUN_STATE_PILL.failed,
  stopped: RUN_STATE_PILL.stopped,
  live: RUN_STATE_PILL.running,
};

// An archived run always names a finished record -- `recentRuns` answers only ended runs, never
// "live" -- so its link carries the run id: one click from a failure on the fleet lands straight
// on that run's own full picture, not on whatever the workflow is doing now.
function recentHref(run: RunEnvelope): string {
  return runHref(run.workflow_id, run.outcome === "live" ? undefined : run.run_id);
}

function hrefFor(card: FleetCard): string {
  return card.kind === "recent" ? recentHref(card.run) : runHref(card.run.workflow_id);
}

function pillFor(card: FleetCard): RunStatePill {
  if (card.kind === "waiting") return RUN_STATE_PILL.waiting;
  if (card.kind === "running") return RUN_STATE_PILL.running;
  if (card.kind === "paused") return RUN_STATE_PILL.paused;
  return OUTCOME_PILL[card.run.outcome];
}

// Every non-param node's fill, off the data the two run shapes actually carry. A live run
// (`RunView`) names its own done/active/failed nodes and the one it waits at, by id. An archived
// run's summary (`RunEnvelope`) carries no such list, only its outcome -- a completed run reads
// every node as ran (the graph is what it says it is), and anything else (failed, stopped) reads
// every node as not run rather than guessing which one broke it.
export function fillOf(card: FleetCard, nodeId: string): GlyphFill {
  if (card.kind === "recent") {
    return card.run.outcome === "completed" ? "ran" : "not-run";
  }
  const run = card.run;
  if (run.failed.includes(nodeId)) return "failed";
  if (run.waiting_at === nodeId) return "waiting";
  if (run.active.includes(nodeId)) return "running";
  if (run.done.includes(nodeId)) return "ran";
  return "not-run";
}

function elapsedFor(card: FleetCard): string {
  if (card.kind === "recent") {
    if (card.run.started_at === "" || card.run.ended_at === "") return "";
    return formatElapsedMs(
      new Date(card.run.ended_at).getTime() - new Date(card.run.started_at).getTime(),
    );
  }
  if (card.run.started_at === "") return "";
  return formatElapsedMs(card.now - new Date(card.run.started_at).getTime());
}

// `null` when no ledger exists for this run yet, so the card shows nothing extra rather than a
// cost of zero.
function costFor(card: FleetCard): string | null {
  const spend = card.run.spend;
  return spend !== null && spend !== undefined ? spendText(spend.cost_usd, spend.unpriced) : null;
}

function GateDetail({ card }: { card: FleetCard }): ReactElement | null {
  if (card.kind === "waiting" && card.run.gate) {
    return (
      <div data-section="decision" className="border-ground border-t px-4 pt-3 pb-3">
        <GatePanel pending={card.run.gate} onDecide={card.onDecide} deciding={card.deciding} />
      </div>
    );
  }
  if (card.kind === "recent") {
    const { gates_orchestrated: orchestrated, gates_auto: auto } = card.run;
    if (orchestrated.length === 0 && auto.length === 0) return null;
    return (
      <div
        data-section="decision"
        className="flex flex-col gap-2 border-ground border-t px-4 pt-3 pb-3"
      >
        {orchestrated.length > 0 ? (
          <ProvenanceChip actor="agent-decided" variant="box" heading="Decided by the agent">
            {provenance.decidedByAgent(orchestrated)}
          </ProvenanceChip>
        ) : null}
        {auto.length > 0 ? (
          <ProvenanceChip actor="automatic" variant="box" heading="Passed automatically">
            {provenance.passedAutomatically(auto)}
          </ProvenanceChip>
        ) : null}
      </div>
    );
  }
  return null;
}

// What the run produced: the checked status in its own words, the counters its author asked for,
// and the artifacts it fixed, by name. An artifact is never read here -- a board full of cards
// would fetch every one -- so each name is a link to its own place on the run's page, which is
// where the bytes are asked for.
function Produced({ card }: { card: FleetCard }): ReactElement | null {
  if (card.kind !== "recent") return null;
  const artifacts = card.run.result?.artifacts ?? [];
  return (
    <>
      <div data-section="result" className="border-ground border-t px-4 pt-3 pb-3">
        <ResultSummary result={card.run.result} compact />
      </div>
      {artifacts.length > 0 ? (
        <div
          data-section="artifacts"
          className="flex flex-col gap-1 border-ground border-t px-4 pt-3 pb-3"
        >
          <p className="text-caption text-label-3">What it left behind</p>
          <ul className="flex flex-wrap gap-x-4 gap-y-1">
            {artifacts.map((artifact) => (
              <li key={artifact.id} className="text-caption text-label-1">
                <Link
                  href={artifactHref(card.run.workflow_id, card.run.run_id, artifact.id)}
                  className="underline decoration-line underline-offset-2 hover:decoration-label-1"
                >
                  {artifact.label}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

// The one inline act this card's kind owns: resume for a paused run, pin for an archived one. Its
// own row, above the execution facts, so a narrow screen shows what a person can do here before it
// shows the ids and the numbers. It leads the row where a thumb reaches it and sits at the card's
// trailing edge once there is width for the rest of the card to breathe, the same place in the
// document either way.
function CardAction({ card }: { card: FleetCard }): ReactElement | null {
  if (card.kind !== "paused" && card.kind !== "recent") return null;
  return (
    <div
      data-section="action"
      className="flex flex-wrap items-center gap-2 border-ground border-t px-4 py-2 sm:justify-end"
    >
      {card.kind === "paused" ? (
        <Button onClick={card.onResume} disabled={card.resuming} className="h-8 px-4 text-caption">
          {card.resuming ? "Resuming" : "Resume"}
        </Button>
      ) : (
        <button
          type="button"
          aria-label={card.run.pinned ? "Unpin this run" : "Pin this run"}
          aria-pressed={card.run.pinned}
          onClick={card.onTogglePin}
          disabled={card.pinning}
          className="inline-flex items-center gap-1.5 rounded border border-line px-2.5 py-1 text-caption text-label-2 hover:bg-well disabled:opacity-50"
        >
          <Star aria-hidden size={14} fill={card.run.pinned ? "currentColor" : "none"} />
          {card.run.pinned ? "Pinned" : "Pin"}
        </button>
      )}
    </div>
  );
}

// How the run ran. A live run names no runner, so only an archived one carries the provenance chip.
function Execution({ card }: { card: FleetCard }): ReactElement {
  const elapsed = elapsedFor(card);
  const cost = costFor(card);
  return (
    <div
      data-section="execution"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 border-ground border-t bg-well px-4 py-2"
    >
      <span className="max-w-full truncate rounded-full border border-line px-2 py-0.5 font-mono text-label-3 text-micro">
        {card.run.workflow_id}
      </span>
      {elapsed !== "" ? (
        <span className="shrink-0 font-mono text-label-2 text-micro">{elapsed}</span>
      ) : null}
      {cost !== null ? (
        <span className="shrink-0 font-mono text-label-2 text-micro">{cost}</span>
      ) : null}
      {card.kind === "recent" ? (
        <ProvenanceChip actor={runnerActor(card.run.runner)} variant="inline">
          {startedBySentence(card.run.runner)}
        </ProvenanceChip>
      ) : null}
    </div>
  );
}

// The run's own shape, folded away: a board is a list of answers, and the graph is what somebody
// opens when an answer needs explaining.
function GraphStrip({ card }: { card: FleetCard }): ReactElement | null {
  const [open, setOpen] = useState(false);
  if (!card.graph) return null;
  return (
    <div data-section="graph" className="border-ground border-t">
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
        Graph
      </button>
      {open ? (
        <div className="px-4 pb-3">
          <MiniGraph graph={card.graph} fillFor={(node) => fillOf(card, node.id)} />
        </div>
      ) : null}
    </div>
  );
}

export function RunCard({ card }: { card: FleetCard }): ReactElement {
  const router = useRouter();
  const pill = pillFor(card);
  const href = hrefFor(card);

  return (
    // The whole card opens the run's own page. A click on any of the card's own controls (the run
    // name's own link, decide, resume, pin) keeps its own meaning instead of navigating.
    // biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it; the keyboard path is the run name's own link.
    <li
      onClick={(event) => {
        if (event.target instanceof Element && event.target.closest("button, input, a, textarea")) {
          return;
        }
        router.push(href);
      }}
      className="cursor-pointer overflow-hidden rounded-xl border border-line bg-surface shadow-sm transition-colors hover:border-line-strong"
    >
      <div data-section="title" className="flex items-center gap-2.5 px-4 pt-2.5 pb-2">
        <Link
          href={href}
          className="min-w-0 flex-1 truncate font-semibold text-body text-label-1 hover:underline"
        >
          {card.name}
        </Link>
        <StatePill tone={pill.tone} size="sm">
          {pill.word}
        </StatePill>
      </div>

      <GateDetail card={card} />
      <Produced card={card} />
      <CardAction card={card} />
      <Execution card={card} />
      <GraphStrip card={card} />
    </li>
  );
}
