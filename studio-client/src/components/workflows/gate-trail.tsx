"use client";

import { cn } from "@/shared/lib/cn";
import type { ReactElement } from "react";

import type { GraphNode, PendingGate, RunEnvelope, StepEvent } from "@/api/studio-types";
import { GatePanel } from "@/components/chat/gate-panel";
import { type GlyphFill, type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";
import { provenance } from "@/components/marks/provenance-chip";
import { StatePill } from "@/components/marks/state-pill";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";

// The gate trail as a spine of decisions, read top to bottom like a log: one row per decision
// this run made, in the order it made them, the open gate last among them, and a gate the run
// never reached named plainly at the very end. A gate visited more than once (a person rejected
// it, the run reran, it was judged again) reads as one row per visit -- the first `gates_rejected`
// finished events are the rejections, the last finished event is the visit that finally decided
// it, so a gate rejected then approved shows both.

export interface GateTrailProps {
  nodes: GraphNode[];
  envelope: RunEnvelope;
  waitingAt: string | null;
  events: StepEvent[];
  pending: PendingGate | null;
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
}

type RowKind = "rejected" | "approved" | "orchestrated" | "auto" | "open" | "not-reached";

interface SpineRow {
  key: string;
  node: GraphNode;
  kind: RowKind;
  at: number | null;
  openEvent: StepEvent | null;
}

function finalKindOf(nodeId: string, envelope: RunEnvelope, waitingAt: string | null): RowKind {
  if (waitingAt === nodeId) return "open";
  if (envelope.gates_approved.includes(nodeId)) return "approved";
  if (envelope.gates_orchestrated.includes(nodeId)) return "orchestrated";
  if (envelope.gates_auto.includes(nodeId)) return "auto";
  if (envelope.gates_rejected[nodeId]) return "rejected";
  return "not-reached";
}

// One entry per visit a gate node made: every rejection first, in the order they happened, then
// the visit that finally decided it (skipped when the gate's own last word was a rejection, since
// that rejection row already said so), then the still-open visit when the run is waiting there.
function buildRows(
  nodes: GraphNode[],
  envelope: RunEnvelope,
  waitingAt: string | null,
  events: StepEvent[],
): SpineRow[] {
  const gateNodes = nodes.filter(
    (node) => node.kind === "gate" || node.kind === "orchestrator-gate",
  );
  const decided: SpineRow[] = [];
  const notReached: SpineRow[] = [];

  for (const node of gateNodes) {
    const finalKind = finalKindOf(node.id, envelope, waitingAt);
    if (finalKind === "not-reached") {
      notReached.push({
        key: `${node.id}-not-reached`,
        node,
        kind: "not-reached",
        at: null,
        openEvent: null,
      });
      continue;
    }

    const finished = events
      .filter((event) => event.node === node.id && event.finished)
      .sort((a, b) => a.seq - b.seq);
    const rejectionCount = envelope.gates_rejected[node.id] ?? 0;
    for (const event of finished.slice(0, rejectionCount)) {
      decided.push({
        key: `${node.id}-${event.seq}`,
        node,
        kind: "rejected",
        at: event.at,
        openEvent: null,
      });
    }

    if (finalKind === "open") {
      const openEvent = events.find((event) => event.node === node.id && !event.finished) ?? null;
      decided.push({
        key: `${node.id}-open`,
        node,
        kind: "open",
        at: openEvent?.at ?? null,
        openEvent,
      });
      continue;
    }

    if (finalKind === "rejected") continue;

    const final = finished[finished.length - 1];
    if (final) {
      decided.push({
        key: `${node.id}-${final.seq}`,
        node,
        kind: finalKind,
        at: final.at,
        openEvent: null,
      });
    }
  }

  decided.sort((a, b) => (a.at ?? 0) - (b.at ?? 0));
  return [...decided, ...notReached];
}

function glyphKindOf(kind: GraphNode["kind"]): GlyphKind {
  return kind === "orchestrator-gate" || kind === "judge" ? "agent-gate" : "person-gate";
}

const GLYPH_FILL: Record<RowKind, GlyphFill> = {
  rejected: "ran",
  approved: "ran",
  orchestrated: "ran",
  auto: "ran",
  open: "waiting",
  "not-reached": "not-run",
};

const HEADING: Record<RowKind, string> = {
  rejected: "Rejected by a person",
  approved: "Approved by a person",
  orchestrated: "Decided by the agent",
  auto: "Passed automatically",
  open: "Waiting on you",
  "not-reached": "Not reached yet",
};

const HEADING_TONE: Record<RowKind, string> = {
  rejected: "text-label-1",
  approved: "text-label-1",
  orchestrated: "text-label-1",
  auto: "text-label-1",
  open: "text-warn-ink",
  "not-reached": "text-label-3",
};

// A person's own decision is the row's heading already; the sentence line is for what the heading
// cannot say (the feedback a rejection carried, who decided when it was not a person), so a
// person's approval or a rejection without feedback has none.
function sentenceFor(row: SpineRow): string {
  switch (row.kind) {
    case "rejected":
      return "";
    case "approved":
      return "";
    case "orchestrated":
      return provenance.decidedByAgent([row.node.id]);
    case "auto":
      return provenance.passedAutomatically([row.node.id]);
    case "open":
      return "The run stops here until you approve or reject it.";
    case "not-reached":
      return "";
  }
}

// The open row's own wait, read off its unfinished event's own start (seconds since the run
// began) against the run's own wall clock start, so it ticks true regardless of how long the run
// ran before it ever reached this gate.
function liveWait(envelope: RunEnvelope, openEvent: StepEvent | null, now: number): string {
  if (openEvent === null || envelope.started_at === "") return "";
  return formatElapsedMs(now - (new Date(envelope.started_at).getTime() + openEvent.at * 1000));
}

function timeFor(row: SpineRow, envelope: RunEnvelope, now: number): string {
  if (row.kind === "not-reached") return "";
  if (row.kind === "open") return liveWait(envelope, row.openEvent, now);
  return row.at !== null ? `${row.at.toFixed(1)}s` : "";
}

interface RowProps {
  row: SpineRow;
  isLast: boolean;
  time: string;
  pending: PendingGate | null;
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
}

function SpineRowView({ row, isLast, time, pending, onDecide, deciding }: RowProps): ReactElement {
  const open = row.kind === "open";
  const sentence = sentenceFor(row);
  const showControls = open && pending !== null && pending.gate_id === row.node.id;

  return (
    <div
      className={cn(
        "relative grid grid-cols-[44px_1fr_120px] items-start gap-3 border-ground border-t px-5 py-3.5 first:border-t-0",
        open && "border-l-4 border-l-warn bg-warn-soft",
      )}
    >
      <div className="-my-3.5 relative flex self-stretch justify-center pt-3.5">
        <span
          aria-hidden
          className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-ground"
          style={{ bottom: isLast ? "50%" : 0 }}
        />
        <span className="relative flex h-6 items-center">
          <NodeGlyph kind={glyphKindOf(row.node.kind)} fill={GLYPH_FILL[row.kind]} size={18} />
        </span>
      </div>
      <div className="min-w-0">
        <span className={cn("font-semibold text-body", HEADING_TONE[row.kind])}>
          {HEADING[row.kind]}
        </span>
        <span className="ml-2 font-mono text-caption text-label-3">{row.node.id}</span>
        {sentence !== "" ? <p className="mt-1.5 text-caption text-label-2">{sentence}</p> : null}
        {showControls && pending !== null ? (
          <div className="mt-2.5">
            <GatePanel pending={pending} onDecide={onDecide} deciding={deciding} chrome="bare" />
          </div>
        ) : null}
      </div>
      <span
        className={cn("text-right font-mono text-caption", open ? "text-warn-ink" : "text-label-3")}
      >
        {time}
      </span>
    </div>
  );
}

export function GateTrail({
  nodes,
  envelope,
  waitingAt,
  events,
  pending,
  onDecide,
  deciding,
}: GateTrailProps): ReactElement | null {
  const rows = buildRows(nodes, envelope, waitingAt, events);
  const now = useNowTick(
    1000,
    rows.some((row) => row.kind === "open"),
  );
  if (rows.length === 0) return null;

  const openCount = rows.filter((row) => row.kind === "open").length;
  const decisionCount = rows.filter(
    (row) => row.kind !== "open" && row.kind !== "not-reached",
  ).length;

  return (
    // `shrink-0` keeps this card at its own content height inside a scrolling flex column: a
    // rounded card needs `overflow-hidden` to clip its own corners, but that also zeroes out its
    // automatic minimum height, so without `shrink-0` a tall sibling elsewhere in the column can
    // still squeeze its rows down to a sliver instead of the column scrolling for the overflow.
    <div className="shrink-0 overflow-hidden rounded-lg border border-line bg-surface shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-3 border-line border-b px-5 py-3.5">
        <h2 className="font-semibold text-label-1 text-subtitle">Gate trail</h2>
        {openCount > 0 ? (
          <StatePill tone="waiting" size="sm">
            <span className="font-mono">{openCount}</span> waiting on you
          </StatePill>
        ) : decisionCount > 0 ? (
          <div className="flex flex-col items-end gap-0.5">
            <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
              Decisions
            </span>
            <span className="font-mono text-body text-label-1">{decisionCount}</span>
          </div>
        ) : null}
      </div>
      <div>
        {rows.map((row, index) => (
          <SpineRowView
            key={row.key}
            row={row}
            isLast={index === rows.length - 1}
            time={timeFor(row, envelope, now)}
            pending={pending}
            onDecide={onDecide}
            deciding={deciding}
          />
        ))}
      </div>
    </div>
  );
}
