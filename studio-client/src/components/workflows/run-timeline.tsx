"use client";

import type { CSSProperties, ReactElement } from "react";

import type { GraphNode, SpendView, StepEvent } from "@/api/studio-types";
import { type GlyphFill, type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";
import { spendText } from "@/lib/cost-text";
import { cn } from "@/shared/lib/cn";

// The run's steps as a time chart: one horizontal lane per node that has run, in the order
// each first started, and a queued lane below for every other node the graph declares (a param
// never runs, so it never gets one). A node's events pair into SPANS in `seq` order -- a start
// (`finished: false`) closes on that node's next `finished: true` -- so a node a gate reruns
// draws several bars in its one lane rather than one bar per node. A span with no closing event
// yet is still running, drawn through to `now`. Clicking a bar, a gate's own marker, or a queued
// lane's placeholder, only names its node to the caller, which owns what "selected" means.

export interface RunTimelineProps {
  events: StepEvent[];
  nodes: GraphNode[];
  onSelectNode: (nodeId: string) => void;
  // Whether the run this chart draws is still going -- gates the live "now" cursor and the
  // ticking clock behind it, off, so an archived run's own chart stays a still picture.
  live?: boolean;
  // The run's own wall clock start, ISO, needed to turn a live tick into a position on the axis
  // (`at` on every event is seconds since this instant, not a timestamp of its own).
  startedAt?: string;
  // This run's own priced cost, folded per step by name -- a lane whose step carries no key
  // here shows no cost, whether because nothing has priced it yet or no ledger exists at all.
  spend?: SpendView | null;
}

export interface Span {
  startAt: number;
  endAt: number | null;
  failed: boolean;
  // Whether this node has a span that starts later than this one -- true for every span but a
  // node's last, so a gate's earlier attempts (each one a rejection, since only a rejection
  // reruns a gate) can carry a mark distinct from its final, undecided-or-approved one.
  hasLaterSpan: boolean;
}

export interface Lane {
  node: string;
  // The step name this lane's node runs, which is what a run's own spend record is keyed by --
  // the node id when the node names no step of its own, and for a lane no graph node declares.
  work: string;
  kind: GraphNode["kind"];
  spans: Span[];
  queued: boolean;
  // Every turn a multi-turn map node runs, base work first, read off the graph's `turns` field --
  // shown as this lane's own secondary line regardless of whether the run has reached it yet.
  // Empty for a lane whose node is not a multi-turn map.
  turnWorks: string[];
}

interface OpenSpan {
  startAt: number;
}

export function buildSpans(events: StepEvent[]): Map<string, Span[]> {
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  const openByNode = new Map<string, OpenSpan>();
  const spansByNode = new Map<string, Span[]>();

  for (const event of ordered) {
    if (!spansByNode.has(event.node)) spansByNode.set(event.node, []);
    const spans = spansByNode.get(event.node);
    if (!spans) continue;

    if (!event.finished) {
      openByNode.set(event.node, { startAt: event.at });
      continue;
    }

    const open = openByNode.get(event.node);
    if (open) {
      spans.push({
        startAt: open.startAt,
        endAt: event.at,
        failed: event.failed,
        hasLaterSpan: false,
      });
      openByNode.delete(event.node);
    } else {
      spans.push({ startAt: event.at, endAt: event.at, failed: event.failed, hasLaterSpan: false });
    }
  }

  for (const [node, open] of openByNode) {
    const spans = spansByNode.get(node);
    if (spans)
      spans.push({ startAt: open.startAt, endAt: null, failed: false, hasLaterSpan: false });
  }

  for (const spans of spansByNode.values()) {
    spans.forEach((span, index) => {
      span.hasLaterSpan = index < spans.length - 1;
    });
  }

  return spansByNode;
}

// Every turn whose own event id differs from the map's own -- the harness's own `node_id` on that
// turn entry. A single-turn map's one turn shares the map's own id and needs no entry here; a
// multi-turn map's every turn, including its first, carries its own dotted `"{id}.{work}"` id
// instead, so all of them fold.
function turnEventIds(node: GraphNode): string[] {
  if (node.kind !== "map" || !node.turns) return [];
  return node.turns.filter((turn) => turn.node_id !== node.id).map((turn) => turn.node_id);
}

// Every turn's own dotted event id, mapped back to the map node whose lane it belongs on -- how a
// turn's own call folds into the graphed lane instead of drawing a stray one of its own.
function turnBaseByEventId(nodes: GraphNode[]): Map<string, GraphNode> {
  const bases = new Map<string, GraphNode>();
  for (const node of nodes) {
    for (const eventId of turnEventIds(node)) bases.set(eventId, node);
  }
  return bases;
}

export function buildLanes(events: StepEvent[], nodes: GraphNode[]): Lane[] {
  const turnBases = turnBaseByEventId(nodes);
  // An event reported under a turn's own id reads as an event of the map node the turn belongs
  // to -- an id naming no such turn is left alone, and draws a lane of its own.
  const folded = events.map((event) => {
    const base = turnBases.get(event.node);
    return base ? { ...event, node: base.id } : event;
  });

  const spansByNode = buildSpans(folded);
  const kindByNode = new Map(nodes.map((node) => [node.id, node.kind]));
  const workByNode = new Map(nodes.map((node) => [node.id, node.work || node.id]));
  const turnWorksByNode = new Map<string, string[]>();
  for (const node of nodes) {
    if (node.kind === "map" && node.turns && node.turns.length > 1) {
      turnWorksByNode.set(
        node.id,
        node.turns.map((turn) => turn.work),
      );
    }
  }

  const seen = new Set<string>();
  const firstSeenOrder: string[] = [];
  for (const event of [...folded].sort((a, b) => a.seq - b.seq)) {
    if (!seen.has(event.node)) {
      seen.add(event.node);
      firstSeenOrder.push(event.node);
    }
  }

  const ranLanes: Lane[] = firstSeenOrder.map((node) => ({
    node,
    work: workByNode.get(node) ?? node,
    kind: kindByNode.get(node) ?? "run",
    spans: spansByNode.get(node) ?? [],
    queued: false,
    turnWorks: turnWorksByNode.get(node) ?? [],
  }));

  const queuedLanes: Lane[] = nodes
    .filter((node) => node.kind !== "param" && !seen.has(node.id))
    .map((node) => ({
      node: node.id,
      work: node.work || node.id,
      kind: node.kind,
      spans: [],
      queued: true,
      turnWorks: turnWorksByNode.get(node.id) ?? [],
    }));

  return [...ranLanes, ...queuedLanes];
}

function maxAt(events: StepEvent[]): number {
  return events.reduce((max, event) => Math.max(max, event.at), 0);
}

const TICK_STEPS = [1, 2, 5, 10, 30, 60];
const MAX_TICKS = 8;

// The smallest step off the clock's usual marks (1/2/5/10/30/60s) that keeps the axis at or
// under `MAX_TICKS` ticks -- doubling past 60s for a run long enough to need it.
export function pickTickStep(totalSpan: number): number {
  if (totalSpan <= 0) return 1;
  for (const step of TICK_STEPS) {
    if (Math.floor(totalSpan / step) + 1 <= MAX_TICKS) return step;
  }
  let step = 60;
  while (Math.floor(totalSpan / step) + 1 > MAX_TICKS) step *= 2;
  return step;
}

export function buildTicks(totalSpan: number): number[] {
  const step = pickTickStep(totalSpan);
  const ticks = [0];
  for (let t = step; t <= totalSpan; t += step) ticks.push(t);
  return ticks;
}

function formatSeconds(seconds: number): string {
  return `${seconds.toFixed(1)}s`;
}

function leftPercent(at: number, totalSpan: number): number {
  return totalSpan > 0 ? (at / totalSpan) * 100 : 0;
}

const MIN_BAR_WIDTH_PERCENT = 0.6;

function widthPercent(startAt: number, endAt: number, totalSpan: number): number {
  const span = Math.max(totalSpan, 0.001);
  return Math.max(((endAt - startAt) / span) * 100, MIN_BAR_WIDTH_PERCENT);
}

function isGateLane(lane: Lane): boolean {
  return lane.kind === "gate" || lane.kind === "orchestrator-gate";
}

// A gate node reads as a person's own diamond, an orchestrator-gate or a judge reads as the
// agent's hexagon (a judge is an agent's own evaluation, not a step a person waits on), anything
// else is a plain step.
function glyphKindOf(kind: GraphNode["kind"]): GlyphKind {
  if (kind === "gate") return "person-gate";
  if (kind === "orchestrator-gate" || kind === "judge") return "agent-gate";
  return "step";
}

function laneGlyphFill(lane: Lane): GlyphFill {
  if (lane.queued) return "not-run";
  const last = lane.spans[lane.spans.length - 1];
  if (!last) return "not-run";
  if (last.endAt === null) return isGateLane(lane) ? "waiting" : "running";
  if (last.failed) return "failed";
  return "ran";
}

interface GutterTime {
  text: string;
  hot: boolean;
}

// The right gutter's own mono time: empty for a lane the run never reached, the still-open
// gate's own live wait while it waits, otherwise the elapsed run time at which the lane's last
// event closed.
function gutterTime(lane: Lane, resolvedNow: number): GutterTime {
  if (lane.queued) return { text: "", hot: false };
  const last = lane.spans[lane.spans.length - 1];
  if (!last) return { text: "", hot: false };
  if (last.endAt === null) {
    return {
      text: formatElapsedMs((resolvedNow - last.startAt) * 1000),
      hot: isGateLane(lane),
    };
  }
  return { text: formatElapsedMs(last.endAt * 1000), hot: false };
}

const DECIMAL_COST = /^(\d+)(?:\.(\d*))?$/;

// Decimal readings added on their own digits rather than as floats, so a figure the wire carries
// exactly is still exact here. A reading that is not a plain non-negative decimal contributes
// nothing, and `null` means nothing readable was given at all.
function addCosts(readings: string[]): string | null {
  const parsed = readings
    .map((reading) => DECIMAL_COST.exec(reading.trim()))
    .filter((read): read is RegExpExecArray => read !== null);
  if (parsed.length === 0) return null;
  const places = Math.max(...parsed.map((read) => (read[2] ?? "").length));
  let total = 0n;
  for (const read of parsed) {
    total += BigInt(read[1] + (read[2] ?? "").padEnd(places, "0"));
  }
  if (places === 0) return total.toString();
  const digits = total.toString().padStart(places + 1, "0");
  const point = digits.length - places;
  return `${digits.slice(0, point)}.${digits.slice(point)}`;
}

// Every step name this lane reads -- every turn's own work for a multi-turn map lane, else the
// node's own work alone. May repeat a name (a multi-turn map can run the same step under two of
// its own turns); callers that count occurrences read it through a `Set` so a repeat inside one
// lane still counts as one reader, not two.
function laneWorkNames(lane: Lane): string[] {
  return lane.turnWorks.length > 0 ? lane.turnWorks : [lane.work];
}

// The step names more than one lane would read, which is what makes a per-lane figure unsayable.
// A run's spend is keyed by STEP, so two nodes running the same step share one bucket and neither
// can be given a share of it. Showing the bucket on both reads as two costs and outruns the run's
// own total, so those lanes show nothing and the total keeps the money.
function sharedWork(lanes: Lane[]): ReadonlySet<string> {
  const seen = new Map<string, number>();
  for (const lane of lanes) {
    // Counted once per LANE, not once per name it holds. A multi-turn map with two turns on the
    // same step is one reader of that step, not two, and must keep its figure.
    for (const name of new Set(laneWorkNames(lane))) {
      seen.set(name, (seen.get(name) ?? 0) + 1);
    }
  }
  return new Set([...seen].filter(([, count]) => count > 1).map(([name]) => name));
}

// This lane's own priced cost. The record is keyed by step name, so a lane reads the step its
// node runs, plus every later turn it folds in beside it, each step counted once however many of
// this lane's names point at it. `null` when no step of this lane carries a key there, whether
// because nothing has priced it yet, no ledger exists for the run, or the step is one another
// lane also runs and no lane can claim it.
function stepCostText(
  lane: Lane,
  spend: SpendView | null | undefined,
  shared: ReadonlySet<string>,
): string | null {
  if (!spend) return null;
  const names = new Set(laneWorkNames(lane));
  const readings: string[] = [];
  let unpriced = 0;
  for (const name of names) {
    if (shared.has(name)) return null;
    const step = spend.steps[name];
    if (step !== undefined) {
      readings.push(step.cost_usd);
      unpriced += step.unpriced;
    }
  }
  const total = addCosts(readings);
  return total === null ? null : spendText(total, unpriced);
}

function stepTitle(lane: Lane, span: Span, now: number): string {
  const duration = formatSeconds((span.endAt ?? now) - span.startAt);
  if (span.endAt === null) return `${lane.node}, ${duration} running.`;
  if (span.failed) return `${lane.node}, ${duration} failed.`;
  return `${lane.node}, ${duration}.`;
}

// A gate's own visit, named by what it was rather than who or what did it -- the trail says who,
// this chart only says when. A visit with a later attempt after it was a rejection; the run's
// still-open visit is the one waiting.
function gateTitle(lane: Lane, span: Span, now: number): string {
  const duration = formatSeconds((span.endAt ?? now) - span.startAt);
  if (span.endAt === null) return `${lane.node}, waiting, ${duration}.`;
  return span.hasLaterSpan
    ? `${lane.node}, rejected, ${duration}.`
    : `${lane.node}, cleared, ${duration}.`;
}

const FAILED_STRIPE_STYLE: CSSProperties = {
  backgroundImage:
    "repeating-linear-gradient(135deg, var(--warn-soft) 0 5px, var(--surface) 5px 10px)",
};

interface BarLook {
  className: string;
  style?: CSSProperties;
}

// A bar's own look, nothing else: done reads solid ink, running solid cobalt and breathing,
// failed the same vermilion stripe every failed mark in this app wears. A gate lane never gets
// a full-height fill -- its own thin dashed segment (a wait for a decision, never a plain run)
// carries every one of its visits, decided or not, with the decision itself read off the marker
// at its end rather than the segment's own color.
function barLook(lane: Lane, span: Span): BarLook {
  if (isGateLane(lane)) {
    return { className: "h-1.5 rounded-full border border-dashed border-warn bg-warn-soft" };
  }
  if (span.endAt === null) {
    return { className: "h-[18px] rounded-full bg-accent motion-safe:animate-pulse" };
  }
  if (span.failed) {
    return { className: "h-[18px] rounded-full border border-warn", style: FAILED_STRIPE_STYLE };
  }
  return { className: "h-[18px] rounded-full bg-label-1" };
}

type LegendSwatchKind = "pill" | "pill-striped" | "pill-dashed" | "glyph-person" | "glyph-agent";
interface LegendEntry {
  word: string;
  kind: LegendSwatchKind;
  tone?: string;
}

// The legend's own words, never a color read alone: Ran and Running share the plain pill, a
// person's and the agent's own clearing carry the same shapes the gate lanes mark their own
// visits with, Waiting is the dashed vermilion wash every open gate wears. Failed only shows up
// once this run actually failed something -- a run with no failure earns no warning in its own
// legend.
function buildLegend(hasFailure: boolean): LegendEntry[] {
  const entries: LegendEntry[] = [
    { word: "Ran", kind: "pill", tone: "bg-label-1" },
    { word: "Running", kind: "pill", tone: "bg-accent" },
    { word: "Cleared by a person", kind: "glyph-person" },
    { word: "Cleared by the agent", kind: "glyph-agent" },
    { word: "Waiting", kind: "pill-dashed", tone: "border-warn bg-warn-soft" },
  ];
  if (hasFailure) entries.push({ word: "Failed", kind: "pill-striped", tone: "border-warn" });
  return entries;
}

function LegendSwatch({ entry }: { entry: LegendEntry }): ReactElement {
  if (entry.kind === "pill") {
    return <span className={cn("h-[10px] w-[22px] rounded-full", entry.tone)} />;
  }
  if (entry.kind === "pill-striped") {
    return (
      <span
        className={cn("h-[10px] w-[22px] rounded-full border", entry.tone)}
        style={FAILED_STRIPE_STYLE}
      />
    );
  }
  if (entry.kind === "pill-dashed") {
    return (
      <span className={cn("h-[10px] w-[22px] rounded-full border-2 border-dashed", entry.tone)} />
    );
  }
  return (
    <span className="flex w-[22px] items-center justify-center">
      <NodeGlyph
        kind={entry.kind === "glyph-person" ? "person-gate" : "agent-gate"}
        fill="ran"
        size={13}
      />
    </span>
  );
}

function Legend({ hasFailure }: { hasFailure: boolean }): ReactElement {
  return (
    <div className="flex flex-wrap items-center justify-end gap-3">
      {buildLegend(hasFailure).map((entry) => (
        <span key={entry.word} className="flex items-center gap-1.5 text-label-2 text-micro">
          <LegendSwatch entry={entry} />
          {entry.word}
        </span>
      ))}
    </div>
  );
}

interface SpanBarProps {
  lane: Lane;
  span: Span;
  now: number;
  onSelectNode: (nodeId: string) => void;
}

function SpanBar({ lane, span, now, onSelectNode }: SpanBarProps): ReactElement {
  const endAt = span.endAt ?? now;
  const left = leftPercent(span.startAt, now);
  const width = widthPercent(span.startAt, endAt, now);
  const gate = isGateLane(lane);
  const look = barLook(lane, span);
  const title = gate ? gateTitle(lane, span, now) : stepTitle(lane, span, now);

  return (
    <button
      type="button"
      onClick={() => onSelectNode(lane.node)}
      title={title}
      aria-label={title}
      className="absolute bottom-1"
      style={{ left: `${left}%`, width: `${width}%` }}
    >
      <span
        className={cn("block w-full min-w-[10px] shadow-sm", look.className)}
        style={look.style}
      />
      {gate && span.endAt !== null ? (
        <NodeGlyph
          kind={glyphKindOf(lane.kind)}
          fill={span.hasLaterSpan ? "failed" : "ran"}
          size={14}
          className="-translate-y-1/2 absolute top-1/2 right-0 translate-x-1/2"
        />
      ) : null}
    </button>
  );
}

interface LaneRowProps {
  lane: Lane;
  now: number;
  eventsEnd: number;
  onSelectNode: (nodeId: string) => void;
  stepCost: string | null;
}

// This lane's own secondary line -- every turn's own work joined in order. `null` for a lane
// whose node runs a single turn, or is not a map at all.
function laneSecondary(lane: Lane): string | null {
  return lane.turnWorks.length > 0 ? lane.turnWorks.join(" then ") : null;
}

function LaneRow({ lane, now, eventsEnd, onSelectNode, stepCost }: LaneRowProps): ReactElement {
  const time = gutterTime(lane, now);
  const secondary = laneSecondary(lane);
  return (
    <div className="grid grid-cols-[150px_1fr_96px] items-center gap-2 border-ground border-t px-1 first:border-t-0">
      <div
        className="flex min-w-0 items-center gap-2 pr-2 text-caption text-label-1"
        title={secondary ? `${lane.node}, ${secondary}` : lane.node}
      >
        <NodeGlyph kind={glyphKindOf(lane.kind)} fill={laneGlyphFill(lane)} size={14} />
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-mono">{lane.node}</span>
          {secondary ? (
            <span className="truncate font-mono text-label-3 text-micro">{secondary}</span>
          ) : null}
        </div>
      </div>
      <div className="relative h-11">
        {lane.queued ? (
          <button
            type="button"
            onClick={() => onSelectNode(lane.node)}
            aria-label={`${lane.node} has not run yet.`}
            title={`${lane.node} has not run yet.`}
            className="absolute inset-y-3.5 rounded-sm border-2 border-line-strong border-dashed bg-well"
            style={{ left: `${leftPercent(eventsEnd, now)}%`, right: 0 }}
          />
        ) : (
          lane.spans.map((span) => (
            <SpanBar
              key={span.startAt}
              lane={lane}
              span={span}
              now={now}
              onSelectNode={onSelectNode}
            />
          ))
        )}
      </div>
      <div className="flex flex-col items-end text-right">
        <span className={cn("font-mono text-caption", time.hot ? "text-warn-ink" : "text-label-3")}>
          {time.text}
        </span>
        {stepCost !== null ? (
          <span className="font-mono text-label-3 text-micro">{stepCost}</span>
        ) : null}
      </div>
    </div>
  );
}

export function RunTimeline({
  events,
  nodes,
  onSelectNode,
  live = false,
  startedAt,
  spend,
}: RunTimelineProps): ReactElement | null {
  const clockMs = useNowTick(1000, live);
  if (events.length === 0) return null;

  const lanes = buildLanes(events, nodes);
  const shared = sharedWork(lanes);
  const eventsEnd = maxAt(events);
  const startedAtMs = startedAt ? new Date(startedAt).getTime() : null;
  const liveNow = live && startedAtMs !== null ? (clockMs - startedAtMs) / 1000 : null;
  const resolvedNow = liveNow ?? eventsEnd;
  const ticks = buildTicks(resolvedNow);
  const trackWidth = Math.max(ticks.length * 90, 400);
  const hasFailure = events.some((event) => event.failed);

  return (
    <div className="max-h-72 overflow-auto border-line border-t bg-surface p-3">
      <div className="mb-2">
        <Legend hasFailure={hasFailure} />
      </div>
      <div style={{ minWidth: `${150 + trackWidth + 96}px` }}>
        <div className="grid grid-cols-[150px_1fr_96px] items-end px-1">
          <span />
          <div className="relative h-5 border-line border-b">
            {ticks.map((tick) => (
              <span
                key={tick}
                className="absolute top-0 -translate-x-1/2 font-mono text-label-3 text-micro"
                style={{ left: `${leftPercent(tick, resolvedNow)}%` }}
              >
                {formatSeconds(tick)}
              </span>
            ))}
            {live ? (
              <span
                className="-translate-x-1/2 absolute top-0 font-mono text-accent-ink text-micro"
                style={{ left: "100%" }}
              >
                now
              </span>
            ) : null}
          </div>
          <span />
        </div>
        <div className="relative">
          <div className="pointer-events-none absolute inset-0 grid grid-cols-[150px_1fr_96px] px-1">
            <span />
            <div className="relative">
              {ticks.map((tick) => (
                <div
                  key={tick}
                  className="absolute inset-y-0 w-px bg-ground"
                  style={{ left: `${leftPercent(tick, resolvedNow)}%` }}
                />
              ))}
              {live ? (
                <div className="absolute inset-y-0 w-[1.5px] bg-accent" style={{ left: "100%" }} />
              ) : null}
            </div>
            <span />
          </div>
          <div className="flex flex-col">
            {lanes.map((lane) => (
              <LaneRow
                key={lane.node}
                lane={lane}
                now={resolvedNow}
                eventsEnd={eventsEnd}
                onSelectNode={onSelectNode}
                stepCost={stepCostText(lane, spend, shared)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
