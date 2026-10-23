"use client";

import {
  BaseEdge,
  Controls,
  type Edge,
  EdgeLabelRenderer,
  type EdgeProps,
  Handle,
  MarkerType,
  Panel,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getBezierPath,
  useEdgesState,
  useNodesInitialized,
  useInternalNode,
  useNodesState,
  useReactFlow,
  useUpdateNodeInternals,
  useViewport,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { cn } from "@/shared/lib/cn";
import {
  Bot,
  Braces,
  Check,
  CircleCheck,
  CircleX,
  Flag,
  Package,
  Repeat,
  Scale,
  Wrench,
  Shield,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";

import type { GraphEdge, GraphNode, WorkflowGraph } from "@/api/studio-types";
import { type GlyphFill, type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";
import type { LaidGraph, LayoutDirection, RejectionArc } from "@/lib/studio-layout";
import { aspectRatioOf, chooseDirection, drawnWires, layoutWorkflow } from "@/lib/studio-layout";

import "./workflow-canvas.css";

// The studio's centerpiece, drawn by two libraries doing what each is for:
// ELK computes the layered layout (edge labels are layout objects that
// reserve space, so they physically cannot stack; a long chain wraps to the
// frame's shape; every wire is routed around the cards), and React Flow owns
// the interaction, pan, zoom, node dragging, fit, controls, minimap, instead
// of a hand-rolled camera.
//
// Custom edges render the PLANNED route (with its collision-free label
// position) as long as both endpoints still sit where the layout put them;
// the moment a node is dragged away, that edge falls back to a live bezier
// with a midpoint label. The plan when it is valid, the truth when it is not.

// How the camera fits a laid graph: the whole drawing, always, with a little air around it,
// scaled to whatever frame holds it -- the same figure whether the frame is the inline card,
// the frame minus a drawer, or the full screen. The one cap: no closer than 1.6x, so a small
// graph is read at a comfortable size rather than blown up to fill the frame.
const FIT = { padding: 0.12, maxZoom: 1.6 };

export interface RunHighlight {
  active: string[];
  done: string[];
  failed: string[];
  waitingAt: string | null;
  // Gate node id -> refusals the viewed run actually heard, the same map the
  // archive's `gates_rejected` carries. Absent entirely for a gate nobody has
  // rejected at yet, never `0`, which would claim a rejection that never
  // happened rather than the truth, that this run has not reached one.
  rejected?: Record<string, number>;
}

export interface WorkflowCanvasProps {
  graph: WorkflowGraph;
  runState?: RunHighlight;
  className?: string;
  onNodeClick?: (node: GraphNode) => void;
  // Told which way the ranks run each time the graph is laid out, so a host can size its frame.
  onOrientation?: (direction: LayoutDirection) => void;
  // "overlay" wears the orientation pills, facts, legend, and zoom controls on the drawing;
  // "none" is the bare drawing, for a full-screen host that wants nothing else on it.
  chrome?: CanvasChrome;
  // A host that mounts this canvas twice (inline and full screen) holds the orientation choice
  // itself and hands it to both, so going full screen keeps the pick. Omitted, the canvas
  // holds its own.
  preferred?: Orientation;
  onPreferredChange?: (value: Orientation) => void;
}

type CanvasChrome = "overlay" | "none";

// What the reader asked of the orientation: the frame's own choice, or one of the two by hand.
export type Orientation = "auto" | LayoutDirection;

type NodeState = "waiting" | "failed" | "active" | "done";
type CardShape = "circle" | "diamond" | "hex" | "rect";
type WireTone = "rejected" | "approved" | "neutral";

type CardNode = Node<
  {
    node: GraphNode;
    state?: NodeState;
    reviews?: string;
    rejectsTo?: string;
    queued: boolean;
    // Which way the ranks run, so a card's handles sit on the sides the wires arrive at.
    direction: LayoutDirection;
  },
  "card"
>;
type WireData = {
  wire: GraphEdge;
  label: string;
  tone: WireTone;
  full: string;
  // The layout's planned route; null until layout has run.
  points: { x: number; y: number }[] | null;
  labelAt: { x: number; y: number } | null;
  // The laid origins of the wire's two nodes — the test for "is the planned route still
  // true". Node positions are state the layout wrote, so a drag moves them and drops the
  // plan; a handle re-measure landing late never does.
  laidAt: { sx: number; sy: number; tx: number; ty: number } | null;
  // A wrapped chain's carriage return: drawn as a direct curve between the cards' facing
  // borders instead of the planned orthogonal detour around the whole drawing.
  sweep: boolean;
  // The rejected-kind edge's own side-arc route, computed from real card
  // boxes; null until layout has run, and unused by every other edge kind.
  arc: RejectionArc | null;
  // How many times the viewed run's gate actually rejected — undefined (not
  // zero) until a run says otherwise, which is what keeps the wiring label
  // "on rejection" instead of the event pip.
  rejectedCount?: number;
  // A dataflow wire's own run-relative ink: cobalt where the run is
  // currently entering, full-opacity ink where it already passed through,
  // faint ink where the run has not reached it yet. Recomputed on every
  // run-state tick, never at layout time, since it depends on nothing the
  // layout measured.
  stroke: string;
  strokeOpacity: number;
  // True once this edge feeds a queued node (see `queued` below) — the
  // whole downstream group fades together, edge included.
  queued: boolean;
};
type WireEdge = Edge<WireData, "wire">;

// Text measurement for edge labels: shared canvas context in a browser, a
// character estimate in jsdom (geometry is not under test there).
let labelContext: CanvasRenderingContext2D | null | undefined;
function measureLabel(text: string): number {
  if (labelContext === undefined) {
    labelContext = document.createElement("canvas").getContext("2d");
    if (labelContext) {
      labelContext.font = "600 10px ui-monospace, SFMono-Regular, Menlo, monospace";
    }
  }
  const measured = labelContext?.measureText(text).width;
  return measured ? Math.ceil(measured) + 8 : text.length * 6.5;
}

// How far before and after each bend an orthogonal route starts to turn: the corner reads as
// a rounded turn, not a sweep, and a bend between two short segments turns tighter rather than
// eating either one.
const BEND_RADIUS = 12;

// The planned route as one path: straight segments with each bend rounded. A route's points
// are its start, every bend, and its end, so every point between two others is a corner.
function orthogonalPath(points: { x: number; y: number }[]): string {
  const [first, ...rest] = points;
  if (!first) return "";
  let path = `M ${first.x} ${first.y}`;
  for (let i = 0; i < rest.length; i++) {
    const corner = rest[i];
    const next = rest[i + 1];
    const previous = i === 0 ? first : rest[i - 1];
    if (!corner || !previous) continue;
    if (!next) {
      path += ` L ${corner.x} ${corner.y}`;
      break;
    }
    const into = { x: corner.x - previous.x, y: corner.y - previous.y };
    const outOf = { x: next.x - corner.x, y: next.y - corner.y };
    const intoLength = Math.hypot(into.x, into.y);
    const outOfLength = Math.hypot(outOf.x, outOf.y);
    const radius = Math.min(BEND_RADIUS, intoLength / 2, outOfLength / 2);
    if (radius <= 0 || intoLength === 0 || outOfLength === 0) {
      path += ` L ${corner.x} ${corner.y}`;
      continue;
    }
    const before = {
      x: corner.x - (into.x / intoLength) * radius,
      y: corner.y - (into.y / intoLength) * radius,
    };
    const after = {
      x: corner.x + (outOf.x / outOfLength) * radius,
      y: corner.y + (outOf.y / outOfLength) * radius,
    };
    path += ` L ${before.x} ${before.y} Q ${corner.x} ${corner.y} ${after.x} ${after.y}`;
  }
  return path;
}

// How far along its wire a condition pip sits: past the rounded first bend, in open channel.
const PIP_STEP = 34;

// The point `distance` along a polyline from its start, or the end for a route shorter than that.
function alongRoute(
  points: { x: number; y: number }[],
  distance: number,
): { x: number; y: number } {
  let remaining = distance;
  for (let i = 0; i < points.length - 1; i++) {
    const from = points[i];
    const to = points[i + 1];
    if (!from || !to) continue;
    const length = Math.hypot(to.x - from.x, to.y - from.y);
    if (length >= remaining && length > 0) {
      const t = remaining / length;
      return { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
    }
    remaining -= length;
  }
  return points[points.length - 1] ?? { x: 0, y: 0 };
}

const KIND_LABEL: Record<string, string> = {
  map: "for each",
  gate: "person gate",
  judge: "judge",
  "orchestrator-gate": "orchestrator gate",
};

// Shape is the redundant channel next to color and border tone: a param reads as a
// trigger-style circle, a run or worker step as a rounded rectangle, a person gate as
// a diamond, an agent gate as a hexagon — the same three marks node-glyph.tsx draws
// for every fleet row, so a reader who learns the shape language once reads it
// everywhere. This record only feeds the card's own `data-shape` attribute now (the
// card body stays one uniform rounded rectangle per the Wire canvas anatomy); the
// glyph in the header is what actually draws the shape.
const SHAPE: Record<GraphNode["kind"], CardShape> = {
  param: "circle",
  run: "rect",
  map: "rect",
  judge: "rect",
  gate: "diamond",
  "orchestrator-gate": "hex",
};

// The title row's leading business icon: the fastest read of what a card IS, before a
// single word of its signature loads. Tinted with the same tone the card's own
// border/background already carry, so the icon reads as reinforcement, not a
// second, disagreeing signal. The orchestrator gate gets its own shield-plus-check
// glyph and accent tone, distinct from the person gate's plain warn shield and the
// judge's scales -- the reader must never have to read the kind chip's words to
// tell the three apart.
const KIND_ICON: Record<GraphNode["kind"], typeof Shield> = {
  param: Braces,
  run: Wrench,
  map: Repeat,
  judge: Scale,
  gate: Shield,
  "orchestrator-gate": ShieldCheck,
};

// What actually runs a step, worn as its icon: a program shows braces, an agent (a briefed
// task, or any step holding tools) shows the bot, a plain registered function the wrench.
// The map's fan-out is already said by its FOR EACH chip, so a map's icon answers the same
// question rather than repeating the fan-out.
function runnerIcon(node: GraphNode): typeof Shield {
  if (node.kind !== "run" && node.kind !== "map") return KIND_ICON[node.kind];
  if (node.source) return Braces;
  if (node.briefing || node.tools.length > 0) return Bot;
  return Wrench;
}
const KIND_ICON_TONE: Record<GraphNode["kind"], string> = {
  param: "text-label-3",
  run: "text-label-2",
  map: "text-violet-500",
  judge: "text-warn-ink",
  gate: "text-warn-ink",
  "orchestrator-gate": "text-accent-ink",
};

// The icon's own tinted chip: each kind's soft half of the same ink/soft token
// pair the rest of the card already uses, so the chip reads as reinforcement
// of the icon tone rather than a third, disagreeing color.
const KIND_CHIP_BG: Record<GraphNode["kind"], string> = {
  param: "bg-muted-soft",
  run: "bg-well",
  map: "bg-violet-500/10",
  judge: "bg-warn-soft",
  gate: "bg-warn-soft",
  "orchestrator-gate": "bg-accent-soft",
};

// The shape glyph next to the business icon, per node-glyph.tsx's own coarser
// vocabulary (a run, a map, and a judge all read as "a node that does work").
function glyphKindOf(kind: GraphNode["kind"]): GlyphKind {
  if (kind === "gate") return "person-gate";
  if (kind === "orchestrator-gate") return "agent-gate";
  return "step";
}

// The glyph's own fill: whatever the run says about this exact node, or the
// glyph's resting "ran" look when there is no run in view at all (a bare
// workflow definition, not yet executed). A run that has not yet reached
// this node fills it "not-run" (a dashed outline) rather than "ran" — the
// dashed outline the canvas draws for anything downstream of where the run
// currently stands.
function glyphFillOf(state: NodeState | undefined, hasRun: boolean): GlyphFill {
  if (state === "waiting") return "waiting";
  if (state === "active") return "running";
  if (state === "failed") return "failed";
  if (state === "done") return "ran";
  return hasRun ? "not-run" : "ran";
}

const GLYPH_KIND_LABEL: Record<GlyphKind, string> = {
  step: "step",
  "person-gate": "person gate",
  "agent-gate": "agent gate",
  "auto-gate": "automatic gate",
};
const GLYPH_KIND_LABEL_TONE: Record<GlyphKind, string> = {
  step: "text-label-3",
  "person-gate": "text-warn-ink",
  "agent-gate": "text-label-2",
  "auto-gate": "text-label-2",
};

// A person-gate header tints its own strip (the wash it wears everywhere it
// waits on a person); an agent-gate header tints toward the well token.
// Every other kind keeps the header's plain default tint, set once in
// workflow-canvas.css.
const HEADER_TINT: Partial<Record<GraphNode["kind"], string>> = {
  gate: "bg-warn-soft border-b-warn/15",
  "orchestrator-gate": "bg-well",
};

function stateOf(node: GraphNode, run?: RunHighlight): NodeState | undefined {
  if (!run) return undefined;
  if (run.waitingAt === node.id) return "waiting";
  if (run.failed.includes(node.id)) return "failed";
  if (run.active.includes(node.id)) return "active";
  if (run.done.includes(node.id)) return "done";
  return undefined;
}

// Every node reachable, forward only (never through a rejection back-edge),
// from the run's current waiting gate, that has not itself picked up a
// state of its own — "a step downstream of a not-yet-reached gate," the
// one case the Wire canvas fades on top of the plain not-run outline.
function queuedKeysOf(graph: WorkflowGraph, run: RunHighlight | undefined): Set<string> {
  const queued = new Set<string>();
  if (!run?.waitingAt) return queued;
  const keyOf = new Map(graph.nodes.map((n) => [n.id, n.key]));
  const startKey = keyOf.get(run.waitingAt);
  if (!startKey) return queued;
  const forward = graph.edges.filter((e) => e.kind === "wire" || e.kind === "map");
  const stateByKey = new Map(graph.nodes.map((n) => [n.key, stateOf(n, run)]));
  const stack = forward.filter((e) => e.from === startKey).map((e) => e.to);
  while (stack.length > 0) {
    const key = stack.pop();
    if (!key || queued.has(key) || stateByKey.get(key)) continue;
    queued.add(key);
    for (const edge of forward) if (edge.from === key) stack.push(edge.to);
  }
  return queued;
}

// A dataflow wire's own run-relative ink: cobalt
// where the run is currently entering this edge (its target is running or
// the very gate it is waiting on), full ink where the run already passed
// through (the target is done or failed), faint ink where it has not
// reached that far yet. With no run in view at all, a wire just reads as a
// normal, fully-inked line — the same "ran" resting look a glyph defaults
// to.
function wireToneOf(
  edge: GraphEdge,
  run: RunHighlight | undefined,
  stateByKey: Map<string, NodeState | undefined>,
): { stroke: string; strokeOpacity: number } {
  if (!run) return { stroke: "var(--label-1)", strokeOpacity: 0.82 };
  const targetState = stateByKey.get(edge.to);
  if (targetState === "active" || targetState === "waiting") {
    return { stroke: "var(--accent)", strokeOpacity: 1 };
  }
  if (targetState === "done" || targetState === "failed") {
    return { stroke: "var(--label-1)", strokeOpacity: 0.82 };
  }
  return { stroke: "var(--label-1)", strokeOpacity: 0.22 };
}

// Raised whenever the card (or a param's circle) deserves visual weight:
// mid-run, awaiting a human, gone wrong, or the one the drawer is currently
// open on.
function isElevated(state: NodeState | undefined, selected: boolean): boolean {
  return state === "active" || state === "waiting" || state === "failed" || selected;
}

// The selection outline keys off the node's own state color rather than a
// separate neutral ring: a selected waiting gate reads vermilion, a selected
// running node reads cobalt, a selected failed node reads vermilion-danger,
// a selected done/idle node reads plain ink. These two attributes drive
// workflow-canvas.css; nothing here is a literal color.
function selectTone(state: NodeState | undefined): "warn" | "accent" | "danger" | "ink" {
  if (state === "waiting") return "warn";
  if (state === "active") return "accent";
  if (state === "failed") return "danger";
  return "ink";
}

// Handles carry their direction as a visible dot on the card edge, not an
// invisible hit target: accent for a source (what flows out), muted for a
// target (what feeds in). Three classes of specificity beat React Flow's own
// default handle rule deterministically — see workflow-canvas.css.
const HANDLE_TARGET_CLASS = "workflow-canvas-handle workflow-canvas-handle--target";
const HANDLE_SOURCE_CLASS = "workflow-canvas-handle workflow-canvas-handle--source";

// A trigger-style param: the circle is the whole "card" for state/selection
// purposes, sized to just the glyph — name and type are labels OUTSIDE the
// shape, stacked below it, the same source/sink terminal anatomy the Wire
// canvas draws for a graph's own inputs (a plain white circle, ink border,
// its name and type stacked below rather than inside it). The handles still
// sit at the outer footprint's own top/bottom edge (not the circle's rim),
// so dagre's planned route and the live handle position agree on the same
// box the layout pass measured; the dot simply lands a couple of caption
// lines below the circle instead of touching it.
function ParamNodeView({ data, selected }: NodeProps<CardNode>) {
  const { node, state, queued, direction } = data;
  const KindIcon = KIND_ICON.param;
  const elevated = isElevated(state, selected);
  return (
    <div
      data-node={node.key}
      data-state={state}
      data-shape="circle"
      data-queued={queued || undefined}
      title={node.briefing || node.detail || undefined}
      className={cn("flex w-24 flex-col items-center gap-1", queued && "opacity-50")}
    >
      <Handle
        type="target"
        position={direction === "LR" ? Position.Left : Position.Top}
        className={HANDLE_TARGET_CLASS}
      />
      <div
        data-kind-icon={node.kind}
        className={cn(
          "workflow-canvas-node relative flex h-12 w-12 items-center justify-center rounded-full border-2 border-label-1 bg-surface transition-[transform,box-shadow,background-color,border-color] duration-150",
          elevated && "workflow-canvas-node--elevated",
          state === "active" &&
            "workflow-canvas-node--running motion-safe:animate-[workflow-canvas-glow_1.8s_ease-in-out_infinite] border-accent bg-accent-soft",
          state === "failed" && "workflow-canvas-node--failed border-danger-ink bg-danger-soft",
          state === "done" && "border-ok-ink/60 bg-ok-soft/60",
          state === "waiting" && "border-warn bg-warn-soft",
          selected && "workflow-canvas-node--selected motion-safe:scale-[1.02]",
        )}
      >
        <KindIcon aria-hidden className={cn("h-4 w-4", KIND_ICON_TONE.param)} />
        {state === "done" ? (
          <span
            data-state-tick="done"
            className="absolute -right-0.5 -top-0.5 inline-flex rounded-full bg-surface p-0.5"
          >
            <Check aria-hidden className="h-2.5 w-2.5 text-ok-ink" />
          </span>
        ) : null}
      </div>
      <div className="max-w-40 truncate text-center font-medium text-label-1 text-subtitle">
        {node.id}
      </div>
      <div
        data-fact="type"
        className="max-w-40 truncate text-center font-mono text-caption text-label-3 uppercase tracking-caps"
      >
        {node.signature}
      </div>
      <Handle
        type="source"
        position={direction === "LR" ? Position.Right : Position.Bottom}
        className={HANDLE_SOURCE_CLASS}
      />
    </div>
  );
}

// A step or gate card's two-zone anatomy: a header bar (the shape glyph, the
// business icon chip, the id in mono with its shape-kind caption below it,
// then the kind's own badges right-aligned) sitting on a tinted strip with
// its own hairline, then a body that is a two-column key-value grid —
// caption labels above their values, never beside them. The card itself is
// one uniform rounded rectangle regardless of kind; shape lives entirely in
// the header's glyph, never in the outer panel's own silhouette.
// `data-shape` stays on the card as its own semantic marker.
function StepCardView({ data, selected }: NodeProps<CardNode>) {
  const { node, state, reviews, rejectsTo, queued, direction } = data;
  const shape = SHAPE[node.kind];
  const KindIcon = runnerIcon(node);
  const glyphKind = glyphKindOf(node.kind);
  const elevated = isElevated(state, selected);
  const tone = selectTone(state);
  // The product is the one type worth the card's own ink: what this step hands the next one.
  // The rest of the signature stays on the tooltip.
  const product = node.signature.includes("→")
    ? (node.signature.split("→").at(-1) ?? "").trim()
    : "";
  // The card wears its identity; its context (signature, charge, tools, routing) lives on the
  // tooltip and one click away in the step reader, so the drawing stays a drawing.
  const context = [
    node.kind === "orchestrator-gate" ? node.briefing : node.signature,
    node.tools.length > 0 ? `tools: ${node.tools.join(", ")}` : null,
    reviews ? `reviews ${reviews}` : null,
    rejectsTo ? `rejects to ${rejectsTo}` : null,
    node.kind === "map" && node.turns && node.turns.length > 1
      ? `then ${node.turns
          .slice(1)
          .map((turn) => turn.work)
          .join(" then ")}`
      : null,
    node.detail || null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <div
      data-node={node.key}
      data-state={state}
      data-shape={shape}
      data-queued={queued || undefined}
      data-selected={selected || undefined}
      data-select-tone={selected ? tone : undefined}
      title={context || undefined}
      className={cn(
        "workflow-canvas-node w-[300px] overflow-hidden rounded-lg border border-line bg-surface transition-[transform,box-shadow,background-color,border-color] duration-150",
        queued && "opacity-50",
        elevated && "workflow-canvas-node--elevated",
        state === "active" &&
          "workflow-canvas-node--running motion-safe:animate-[workflow-canvas-glow_1.8s_ease-in-out_infinite]",
        state === "failed" && "workflow-canvas-node--failed",
        node.unknown && "border-dashed border-danger-ink/60",
        selected && [
          "workflow-canvas-node--selected motion-safe:scale-[1.02]",
          tone === "warn" && "border-warn",
          tone === "accent" && "border-accent",
          tone === "danger" && "border-danger-ink",
          tone === "ink" && "border-label-1",
        ],
      )}
    >
      <Handle
        type="target"
        position={direction === "LR" ? Position.Left : Position.Top}
        className={HANDLE_TARGET_CLASS}
      />

      {/* Two rows: the identity row gives the id the card's full width so a name is never cut
          to three letters by the badges, which sit on their own row below when there are any. */}
      <div
        data-zone="header"
        className={cn(
          "workflow-canvas-node__header flex flex-col gap-1 px-3 py-2",
          HEADER_TINT[node.kind],
        )}
      >
        <div className="flex items-center gap-2">
          <NodeGlyph
            kind={glyphKind}
            fill={glyphFillOf(state, Boolean(state) || queued)}
            size={32}
            className="shrink-0"
          />
          <span
            data-kind-icon={node.kind}
            className={cn(
              "inline-flex shrink-0 items-center justify-center rounded-sm p-1",
              KIND_CHIP_BG[node.kind],
            )}
          >
            <KindIcon aria-hidden className={cn("h-4.5 w-4.5", KIND_ICON_TONE[node.kind])} />
          </span>
          <div className="flex min-w-0 flex-col">
            <span className="break-all font-mono font-semibold text-label-1 text-subtitle">
              {node.id}
            </span>
            <span
              className={cn(
                "text-caption uppercase tracking-caps",
                GLYPH_KIND_LABEL_TONE[glyphKind],
              )}
            >
              {GLYPH_KIND_LABEL[glyphKind]}
            </span>
          </div>
          {state === "done" ? (
            <span data-state-tick="done" className="ml-auto inline-flex shrink-0">
              <Check aria-hidden className="h-3 w-3 text-ok-ink" />
            </span>
          ) : null}
        </div>
        {(node.work && node.work !== node.id) ||
        KIND_LABEL[node.kind] ||
        node.brought ||
        node.is_output ? (
          <div className="flex flex-wrap items-center gap-1">
            {node.work && node.work !== node.id ? (
              <span className="rounded-sm bg-muted-soft px-1.5 py-0.5 text-caption text-muted-ink uppercase tracking-caps">
                {node.work}
              </span>
            ) : null}
            {KIND_LABEL[node.kind] ? (
              <span className="rounded-sm bg-muted-soft px-1.5 py-0.5 text-caption text-muted-ink uppercase tracking-caps">
                {KIND_LABEL[node.kind]}
              </span>
            ) : null}
            {node.brought ? (
              <span
                data-carried
                title="This step is inline: its definition travels with the workflow, not one of this app's own registered functions."
                className="inline-flex items-center rounded-sm bg-accent-soft p-1 text-accent-ink"
              >
                <Package aria-label="saved with the workflow" className="h-3.5 w-3.5" />
              </span>
            ) : null}
            {node.is_output ? (
              <span className="flex items-center gap-0.5 rounded-sm bg-ok-soft px-1.5 py-0.5 text-caption text-ok-ink uppercase tracking-caps">
                <Flag aria-hidden className="h-2.5 w-2.5" />
                result
              </span>
            ) : null}
          </div>
        ) : null}
        {product ? (
          <div data-fact="product" className="truncate font-mono text-caption text-label-2">
            {`gives ${product}`}
          </div>
        ) : null}
      </div>

      <Handle
        type="source"
        position={direction === "LR" ? Position.Right : Position.Bottom}
        className={HANDLE_SOURCE_CLASS}
      />
    </div>
  );
}

function CardNodeView(props: NodeProps<CardNode>) {
  return props.data.node.kind === "param" ? (
    <ParamNodeView {...props} />
  ) : (
    <StepCardView {...props} />
  );
}

// The map fan-out's own dashed violet wire stays a fixed tone; the plain
// dataflow "wire" kind is computed live per edge instead (see wireToneOf),
// and a rejection is always the same vermilion regardless of run state.
const MAP_EDGE_STROKE = "var(--color-violet-400)";
const REJECTED_EDGE_STROKE = "var(--warn)";

// The condition badges sitting on a wire: a rejection and an approved pass-through
// read in the amber/green tones the cards already use for waiting and done, plus a
// neutral badge for every other wire label (an argument name, a fan-out count).
const BADGE_TONE: Record<WireTone, string> = {
  rejected: "bg-warn-soft text-warn-ink ring-warn-ink/40",
  approved: "bg-ok-soft text-ok-ink ring-ok-ink/40",
  neutral: "bg-well text-label-2 ring-line",
};

// The condition pip's word, below its circle: the tone's ink alone, no chip of
// its own — the circle already carries the color, the word only needs to be
// legible next to it.
const BADGE_INK: Record<WireTone, string> = {
  rejected: "text-warn-ink",
  approved: "text-ok-ink",
  neutral: "text-label-2",
};

type Point = { x: number; y: number };

// The rejection arc's three straight segments, rendered as one path so the
// dash pattern runs continuously; `stroke-linejoin: round` (set where this is
// used) rounds the two bends into a bow without changing where the line
// actually sits, so the clearance the geometry computed is never traded away
// for looks.
function arcPath(points: readonly [Point, Point, Point, Point]): string {
  const [p0, p1, p2, p3] = points;
  return `M ${p0.x} ${p0.y} L ${p1.x} ${p1.y} L ${p2.x} ${p2.y} L ${p3.x} ${p3.y}`;
}

function RejectionEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  planned,
  data,
}: EdgeProps<WireEdge> & { planned: boolean }) {
  if (!data) return null;
  // The bow's own offset survives even off the planned route: a dragged node
  // still gets a path built from the live handle positions, just carried out
  // to the same line the layout already proved clear of the column (a fixed x
  // when the ranks run down the page) or the row (a fixed y when they run across).
  const bow = data.arc?.points[1];
  const bowsSideways = data.arc ? data.arc.points[1].x === data.arc.points[2].x : true;
  let path: string;
  let apex: Point;
  if (planned && data.arc) {
    path = arcPath(data.arc.points);
    apex = data.arc.apex;
  } else if (bow !== undefined) {
    const live: [Point, Point, Point, Point] = bowsSideways
      ? [
          { x: sourceX, y: sourceY },
          { x: bow.x, y: sourceY },
          { x: bow.x, y: targetY },
          { x: targetX, y: targetY },
        ]
      : [
          { x: sourceX, y: sourceY },
          { x: sourceX, y: bow.y },
          { x: targetX, y: bow.y },
          { x: targetX, y: targetY },
        ];
    path = arcPath(live);
    apex = bowsSideways
      ? { x: bow.x, y: (sourceY + targetY) / 2 }
      : { x: (sourceX + targetX) / 2, y: bow.y };
  } else {
    const [bezier, midX, midY] = getBezierPath({
      sourceX,
      sourceY,
      targetX,
      targetY,
      sourcePosition,
      targetPosition,
    });
    path = bezier;
    apex = { x: midX, y: midY };
  }

  const count = data.rejectedCount && data.rejectedCount > 0 ? data.rejectedCount : undefined;
  return (
    <g data-edge={id} className={cn(data.queued && "opacity-50")}>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        className="workflow-canvas-wire-rejected"
        style={{
          stroke: REJECTED_EDGE_STROKE,
          strokeWidth: 2.5,
          strokeDasharray: "9 7",
          strokeLinecap: "round",
          strokeLinejoin: "round",
        }}
      />
      <EdgeLabelRenderer>
        {/* The arc's structure always speaks: "on rejection" names the wiring
            whether or not this run ever used it — a plain wiring label, not a
            condition pip, so it keeps the flat pill shape. Only a run whose
            gate actually rejected upgrades to the circle-glyph-plus-word-below
            pip. */}
        {count ? (
          <span
            title={data.full}
            data-pip="rejected"
            className="pointer-events-auto absolute flex flex-col items-center gap-0.5"
            style={{ transform: `translate(-50%, -50%) translate(${apex.x}px, ${apex.y}px)` }}
          >
            <span
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full ring-1",
                BADGE_TONE.rejected,
              )}
            >
              <CircleX aria-hidden className="h-3.5 w-3.5 shrink-0" />
            </span>
            <span
              className={cn(
                "whitespace-nowrap font-mono font-semibold text-caption",
                BADGE_INK.rejected,
              )}
            >
              {`rejected${count > 1 ? ` (${count})` : ""}`}
            </span>
          </span>
        ) : (
          <span
            title={data.full}
            data-arc-label="on-rejection"
            className={cn(
              "pointer-events-auto absolute whitespace-nowrap rounded-full px-1.5 py-0.5 font-mono font-semibold text-caption ring-1",
              BADGE_TONE.neutral,
            )}
            style={{ transform: `translate(-50%, -50%) translate(${apex.x}px, ${apex.y}px)` }}
          >
            on rejection
          </span>
        )}
      </EdgeLabelRenderer>
    </g>
  );
}

// Where a wire attaches when its nodes have left the plan: on each card's border, along the
// line between the two card centers, entering perpendicular-ish to the side it lands on. A
// dragged card's wire then reads as a straight reach between the two cards, not an S-curve
// forced through the handles the layout had picked for a different arrangement.
interface FloatingEnds {
  sx: number;
  sy: number;
  tx: number;
  ty: number;
  sourcePosition: Position;
  targetPosition: Position;
}

interface NodeRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

// The point where the segment from this rectangle's center toward `toward` crosses its border.
function borderPoint(rect: NodeRect, toward: { x: number; y: number }): { x: number; y: number } {
  const cx = rect.x + rect.width / 2;
  const cy = rect.y + rect.height / 2;
  const dx = toward.x - cx;
  const dy = toward.y - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const scale = 1 / Math.max(Math.abs(dx) / (rect.width / 2), Math.abs(dy) / (rect.height / 2));
  return { x: cx + dx * scale, y: cy + dy * scale };
}

// Which side of the rectangle a border point sits on, for the bezier's own tangent.
function sideOf(rect: NodeRect, point: { x: number; y: number }): Position {
  const left = Math.abs(point.x - rect.x);
  const right = Math.abs(point.x - (rect.x + rect.width));
  const top = Math.abs(point.y - rect.y);
  const bottom = Math.abs(point.y - (rect.y + rect.height));
  const nearest = Math.min(left, right, top, bottom);
  if (nearest === left) return Position.Left;
  if (nearest === right) return Position.Right;
  return nearest === top ? Position.Top : Position.Bottom;
}

// A node's laid rectangle, or null while React Flow has not measured it (jsdom, first frame).
function rectOf(node: ReturnType<typeof useInternalNode>): NodeRect | null {
  if (!node?.measured?.width || !node.measured.height) return null;
  return {
    x: node.internals.positionAbsolute.x,
    y: node.internals.positionAbsolute.y,
    width: node.measured.width,
    height: node.measured.height,
  };
}

function floatingEnds(source: NodeRect, target: NodeRect): FloatingEnds {
  const from = borderPoint(source, {
    x: target.x + target.width / 2,
    y: target.y + target.height / 2,
  });
  const to = borderPoint(target, {
    x: source.x + source.width / 2,
    y: source.y + source.height / 2,
  });
  return {
    sx: from.x,
    sy: from.y,
    tx: to.x,
    ty: to.y,
    sourcePosition: sideOf(source, from),
    targetPosition: sideOf(target, to),
  };
}

function WireEdgeView({
  id,
  source,
  target,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  data,
}: EdgeProps<WireEdge>) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  if (!data) return null;
  const planned =
    data.points &&
    data.laidAt &&
    sourceNode &&
    targetNode &&
    Math.abs(sourceNode.internals.positionAbsolute.x - data.laidAt.sx) < 1 &&
    Math.abs(sourceNode.internals.positionAbsolute.y - data.laidAt.sy) < 1 &&
    Math.abs(targetNode.internals.positionAbsolute.x - data.laidAt.tx) < 1 &&
    Math.abs(targetNode.internals.positionAbsolute.y - data.laidAt.ty) < 1;

  // A sweep (a wrapped chain's carriage return) never takes its planned points: it is drawn
  // like a floating wire, a direct curve between the cards' facing borders.
  const onPlan = planned && !data.sweep;

  // Off the plan, the wire attaches floating on the card borders when both cards have a
  // measured box; the handle points are the jsdom-and-not-yet-measured fallback.
  const sourceRect = rectOf(sourceNode);
  const targetRect = rectOf(targetNode);
  const ends: FloatingEnds =
    !onPlan && sourceRect && targetRect
      ? floatingEnds(sourceRect, targetRect)
      : { sx: sourceX, sy: sourceY, tx: targetX, ty: targetY, sourcePosition, targetPosition };

  if (data.wire.kind === "rejected") {
    return (
      <RejectionEdge
        id={id}
        source={source}
        target={target}
        sourceX={sourceX}
        sourceY={sourceY}
        targetX={targetX}
        targetY={targetY}
        sourcePosition={sourcePosition}
        targetPosition={targetPosition}
        markerEnd={markerEnd}
        planned={Boolean(planned)}
        data={data}
      />
    );
  }

  // The condition pip belongs to the wire's source end — never the mid-path point a plain
  // data-carrying label uses — but stepped out along the route, clear of the card's own face:
  // the node layer paints above the label layer, so a pip sitting exactly on the handle is
  // half-swallowed by the card it leaves.
  const pipAt =
    onPlan && data.points
      ? alongRoute(data.points, PIP_STEP)
      : alongRoute(
          [
            { x: ends.sx, y: ends.sy },
            { x: ends.tx, y: ends.ty },
          ],
          PIP_STEP,
        );

  let path: string;
  let labelX: number | undefined;
  let labelY: number | undefined;
  if (onPlan && data.points) {
    path = orthogonalPath(data.points);
    labelX = data.labelAt?.x;
    labelY = data.labelAt?.y;
  } else {
    const [bezier, midX, midY] = getBezierPath({
      sourceX: ends.sx,
      sourceY: ends.sy,
      targetX: ends.tx,
      targetY: ends.ty,
      sourcePosition: ends.sourcePosition,
      targetPosition: ends.targetPosition,
    });
    path = bezier;
    if (data.label) {
      labelX = midX;
      labelY = midY;
    }
  }

  return (
    <g data-edge={id} className={cn(data.queued && "opacity-50")}>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={{
          stroke: data.wire.kind === "map" ? MAP_EDGE_STROKE : data.stroke,
          strokeOpacity: data.wire.kind === "map" ? undefined : data.strokeOpacity,
          strokeWidth: 2.5,
          strokeDasharray: data.wire.kind === "map" ? "1.5 4" : undefined,
          strokeLinecap: "round",
        }}
      />
      {data.label && data.tone !== "neutral" ? (
        // A condition pip, not a floating label: it sits on the connection point it
        // describes, never mid-path, so "approved"/"rejected" always reads as a
        // property of that specific wire end, not a caption drifting between nodes.
        // The circular glyph carries the color; the word sits below it, plain.
        <EdgeLabelRenderer>
          <span
            title={data.full}
            data-pip={data.tone}
            className="pointer-events-auto absolute flex flex-col items-center gap-0.5"
            style={{ transform: `translate(-50%, 4px) translate(${pipAt.x}px, ${pipAt.y}px)` }}
          >
            <span
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full ring-1",
                BADGE_TONE[data.tone],
              )}
            >
              {data.tone === "approved" ? (
                <CircleCheck aria-hidden className="h-3.5 w-3.5 shrink-0" />
              ) : (
                <CircleX aria-hidden className="h-3.5 w-3.5 shrink-0" />
              )}
            </span>
            <span
              className={cn(
                "whitespace-nowrap font-mono font-semibold text-caption",
                BADGE_INK[data.tone],
              )}
            >
              {data.label}
            </span>
          </span>
        </EdgeLabelRenderer>
      ) : data.label && labelX !== undefined && labelY !== undefined ? (
        <EdgeLabelRenderer>
          <span
            title={data.full}
            data-badge={data.tone}
            className={cn(
              "pointer-events-auto absolute rounded-full px-1.5 py-0.5 font-mono font-semibold text-caption ring-1",
              BADGE_TONE.neutral,
            )}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {data.label}
          </span>
        </EdgeLabelRenderer>
      ) : null}
    </g>
  );
}

const NODE_TYPES = { card: CardNodeView };
const EDGE_TYPES = { wire: WireEdgeView };

// The rectangle a laid drawing occupies, measured from the content itself: every card box,
// every route's points, and every rejection bow with the label on its apex. Never the layout
// engine's own reported size -- a wrapped layout reports its aspect-ratio target box, which can
// carry hundreds of pixels of empty margin the camera would then aim at instead of the cards.
function drawingBounds(laid: LaidGraph): { x: number; y: number; width: number; height: number } {
  let left = Number.POSITIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  let right = Number.NEGATIVE_INFINITY;
  let bottom = Number.NEGATIVE_INFINITY;
  const cover = (x: number, y: number, reach = 0) => {
    left = Math.min(left, x - reach);
    top = Math.min(top, y - reach);
    right = Math.max(right, x + reach);
    bottom = Math.max(bottom, y + reach);
  };
  for (const box of laid.nodes.values()) {
    cover(box.x, box.y);
    cover(box.x + box.width, box.y + box.height);
  }
  for (const edge of laid.edges) {
    for (const point of edge.points) cover(point.x, point.y);
    if (edge.arc)
      for (const point of [...edge.arc.points, edge.arc.apex])
        cover(point.x, point.y, ARC_LABEL_REACH);
  }
  if (!Number.isFinite(left)) return { x: 0, y: 0, width: laid.width, height: laid.height };
  const air = 12;
  return {
    x: left - air,
    y: top - air,
    width: right - left + air * 2,
    height: bottom - top + air * 2,
  };
}

// How far a bow's "on rejection" pill reaches past the apex it is centred on.
const ARC_LABEL_REACH = 48;

// Which side of a card its wires leave from and arrive at, for one direction of the ranks.
function handleSides(direction: LayoutDirection): {
  sourcePosition: Position;
  targetPosition: Position;
} {
  return direction === "LR"
    ? { sourcePosition: Position.Right, targetPosition: Position.Left }
    : { sourcePosition: Position.Bottom, targetPosition: Position.Top };
}

function toFlowNodes(
  graph: WorkflowGraph,
  run?: RunHighlight,
  direction: LayoutDirection = "TB",
): CardNode[] {
  const idOf = new Map(graph.nodes.map((n) => [n.key, n.id]));
  const queuedKeys = queuedKeysOf(graph, run);
  return graph.nodes.map((node) => {
    // A gate's own facts ride the surrounding edges, not the node payload: the
    // wire feeding it is what it reviews, the rejected-kind edge leaving it is
    // where a rejection reruns from. An orchestrator gate is wired identically --
    // the same "wire" and "rejected" edge kinds arrive from the payload either way.
    const isGateKind = node.kind === "gate" || node.kind === "orchestrator-gate";
    const reviewWire = isGateKind
      ? graph.edges.find((e) => e.to === node.key && e.kind === "wire")
      : undefined;
    const rejectWire = isGateKind
      ? graph.edges.find((e) => e.from === node.key && e.kind === "rejected")
      : undefined;
    return {
      id: node.key,
      type: "card" as const,
      position: { x: 0, y: 0 },
      ...handleSides(direction),
      data: {
        node,
        direction,
        state: stateOf(node, run),
        reviews: reviewWire ? (idOf.get(reviewWire.from) ?? reviewWire.from) : undefined,
        rejectsTo: rejectWire ? (idOf.get(rejectWire.to) ?? rejectWire.to) : undefined,
        queued: queuedKeys.has(node.key),
      },
    };
  });
}

function toFlowEdges(graph: WorkflowGraph, run?: RunHighlight): WireEdge[] {
  const kindOf = new Map(graph.nodes.map((n) => [n.key, n.kind]));
  const idOf = new Map(graph.nodes.map((n) => [n.key, n.id]));
  const stateByKey = new Map(graph.nodes.map((n) => [n.key, stateOf(n, run)]));
  const queuedKeys = queuedKeysOf(graph, run);
  return drawnWires(graph.nodes, graph.edges).map(({ edge, index }) => {
    // A gate's outgoing wire (as opposed to its rejected-kind edge) is the
    // approved, pass-through path — the backend never spells that out in the
    // wire's own label, so the badge names it here. An orchestrator gate's own
    // pass-through wire reads the same way.
    const fromKind = kindOf.get(edge.from);
    const approved =
      edge.kind === "wire" && (fromKind === "gate" || fromKind === "orchestrator-gate");
    const { stroke, strokeOpacity } = wireToneOf(edge, run, stateByKey);
    const markerColor = edge.kind === "rejected" ? REJECTED_EDGE_STROKE : stroke;
    return {
      id: `${edge.from}->${edge.to}#${index}`,
      source: edge.from,
      target: edge.to,
      type: "wire" as const,
      markerEnd: { type: MarkerType.ArrowClosed, color: markerColor, width: 14, height: 14 },
      data: {
        wire: edge,
        label: approved
          ? "approved"
          : edge.label && !edge.from.startsWith("param:")
            ? (edge.label.split(":")[0] ?? "").trim()
            : "",
        tone: edge.kind === "rejected" ? "rejected" : approved ? "approved" : "neutral",
        full: edge.label,
        points: null,
        labelAt: null,
        laidAt: null,
        sweep: false,
        arc: null,
        rejectedCount:
          edge.kind === "rejected" ? run?.rejected?.[idOf.get(edge.from) ?? edge.from] : undefined,
        stroke,
        strokeOpacity,
        queued: queuedKeys.has(edge.to),
      },
    };
  });
}

interface FlowInnerProps extends WorkflowCanvasProps {
  preferred: Orientation;
  // Reports which way the ranks actually run once laid out, so the Auto pill can say so.
  onLaidOut: (direction: LayoutDirection) => void;
  // What sits on top of the drawing: the overlay pills and captions, or nothing at all.
  chrome: CanvasChrome;
  laidOut: LayoutDirection;
  onPreferred: (value: Orientation) => void;
}

function FlowInner({
  graph,
  runState,
  className,
  onNodeClick,
  preferred,
  onLaidOut,
  chrome,
  laidOut,
  onPreferred,
}: FlowInnerProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<CardNode>(toFlowNodes(graph, runState));
  // The card sizes the current layout was computed from. A browser can re-measure a card after
  // the layout ran (a font arriving late re-metrics every label): when a painted size drifts
  // from the size the layout used, the plan is stale -- routes fall back to loose curves and
  // the fit aims wrong -- so the drift releases the layout key and asks for a fresh pass.
  const laidSizesRef = useRef<Map<string, { width: number; height: number }>>(new Map());
  const [relayoutStamp, setRelayoutStamp] = useState(0);
  const onNodesChangeChecked: typeof onNodesChange = (changes) => {
    onNodesChange(changes);
    for (const change of changes) {
      if (change.type !== "dimensions" || !change.dimensions) continue;
      const laidWith = laidSizesRef.current.get(change.id);
      if (!laidWith) continue;
      if (
        Math.abs(laidWith.width - change.dimensions.width) > 1.5 ||
        Math.abs(laidWith.height - change.dimensions.height) > 1.5
      ) {
        laidSignatureRef.current = null;
        laidSizesRef.current = new Map();
        setRelayoutStamp((stamp) => stamp + 1);
        return;
      }
    }
  };
  const [edges, setEdges, onEdgesChange] = useEdgesState<WireEdge>(toFlowEdges(graph, runState));
  const nodesInitialized = useNodesInitialized();
  const { getNodes, fitView, fitBounds, getZoom, zoomTo } = useReactFlow();
  const updateNodeInternals = useUpdateNodeInternals();
  const { x: viewX, y: viewY, zoom } = useViewport();
  const laidSignatureRef = useRef<string | null>(null);
  // The laid drawing's own bounds, routes and bows included, which is what the camera fits: a
  // fit to the cards alone would cut the wrap-around routes off at the frame's edges.
  const laidBoundsRef = useRef<{ x: number; y: number; width: number; height: number } | null>(
    null,
  );
  // The frame the graph has to fit, measured when the layout runs, to pick the orientation.
  const frameRef = useRef<HTMLDivElement>(null);
  // Fits the camera to the laid drawing: its bounds with a little air, and no closer than the
  // fit's own zoom cap. Pure arithmetic over the measured bounds, on purpose: a camera that
  // reads the painted pixels back corrects against frames that lag the state on a slow machine
  // and shoves the drawing out of the frame. When the bounds are stale the layout pass that
  // refreshes them refits, so the last fit always aims true. Before any layout has landed,
  // the cards are all there is to fit.
  const fitDrawing = useCallback(async () => {
    const bounds = laidBoundsRef.current;
    if (bounds === null) {
      await fitView(FIT);
      return;
    }
    await fitBounds(bounds, { padding: FIT.padding });
    if (getZoom() > FIT.maxZoom) await zoomTo(FIT.maxZoom);
  }, [fitView, fitBounds, getZoom, zoomTo]);
  // Counts layouts, so the settle effect below runs once after each one has rendered.
  const [laidStamp, setLaidStamp] = useState(0);

  const signature = useMemo(
    () => JSON.stringify([graph.nodes, graph.edges]),
    [graph.nodes, graph.edges],
  );

  // A new graph resets the flow to unmeasured nodes at the origin; run-state
  // changes only repaint card states, never the layout.
  // biome-ignore lint/correctness/useExhaustiveDependencies: signature IS the graph identity
  useEffect(() => {
    if (laidSignatureRef.current?.startsWith(`${signature}|`)) return;
    setNodes(toFlowNodes(graph, runState, "TB"));
    setEdges(toFlowEdges(graph, runState));
  }, [signature, setNodes, setEdges]);

  // A run-state tick repaints every node's own state and queued fade, never
  // the layout already computed.
  useEffect(() => {
    const queuedKeys = queuedKeysOf(graph, runState);
    setNodes((current) =>
      current.map((flowNode) => ({
        ...flowNode,
        data: {
          ...flowNode.data,
          state: stateOf(flowNode.data.node, runState),
          queued: queuedKeys.has(flowNode.id),
        },
      })),
    );
  }, [runState, graph, setNodes]);

  // A run-state tick also repaints every wire's own ink (cobalt where it is
  // currently entering, ink where it already passed, faint ink where it has
  // not been reached), a rejection edge's rejected count, and the queued
  // fade on any edge feeding a queued node — none of it a layout fact, so
  // none of it touches the geometry the layout effect below computed once.
  useEffect(() => {
    const idOf = new Map(graph.nodes.map((n) => [n.key, n.id]));
    const stateByKey = new Map(graph.nodes.map((n) => [n.key, stateOf(n, runState)]));
    const queuedKeys = queuedKeysOf(graph, runState);
    setEdges((current) =>
      current.map((flowEdge) => {
        if (!flowEdge.data) return flowEdge;
        const { stroke, strokeOpacity } = wireToneOf(flowEdge.data.wire, runState, stateByKey);
        const gateId = idOf.get(flowEdge.data.wire.from) ?? flowEdge.data.wire.from;
        const markerColor = flowEdge.data.wire.kind === "rejected" ? REJECTED_EDGE_STROKE : stroke;
        return {
          ...flowEdge,
          markerEnd: { type: MarkerType.ArrowClosed, color: markerColor, width: 14, height: 14 },
          data: {
            ...flowEdge.data,
            stroke,
            strokeOpacity,
            queued: queuedKeys.has(flowEdge.data.wire.to),
            rejectedCount:
              flowEdge.data.wire.kind === "rejected" ? runState?.rejected?.[gateId] : undefined,
          },
        };
      }),
    );
  }, [runState, graph, setEdges]);

  // Once React Flow has measured the cards, the graph is laid out with the real sizes and the
  // camera fits the result. The user only ever sees the second frame. The layout is
  // asynchronous: a result that lands after the graph or the orientation changed is dropped,
  // and the key it claimed is released so the next run lays out again.
  // biome-ignore lint/correctness/useExhaustiveDependencies: runs when measurement lands for this signature
  useEffect(() => {
    const layoutKey = `${signature}|${preferred}`;
    if (!nodesInitialized || laidSignatureRef.current === layoutKey || graph.nodes.length === 0)
      return;
    laidSignatureRef.current = layoutKey;
    let cancelled = false;

    const measured = new Map(getNodes().map((flowNode) => [flowNode.id, flowNode.measured]));
    laidSizesRef.current = new Map(
      [...measured].flatMap(([key, size]) =>
        size?.width && size.height ? [[key, { width: size.width, height: size.height }]] : [],
      ),
    );
    const measureNode = (node: GraphNode) => {
      const size = measured.get(node.key);
      if (size?.width && size?.height) return { width: size.width, height: size.height };
      // Unmeasured fallback only: a param's circle-plus-two-caption-lines
      // footprint is taller and narrower than a step card's, so it gets its
      // own guess instead of the generic one.
      return node.kind === "param" ? { width: 96, height: 88 } : { width: 60, height: 32 };
    };
    const frame = {
      width: frameRef.current?.clientWidth ?? 0,
      height: frameRef.current?.clientHeight ?? 0,
    };
    const aspect = aspectRatioOf(frame);
    // Both orientations are laid out to the frame's shape and the frame keeps the one it fits
    // at the larger zoom.
    Promise.all([
      layoutWorkflow(graph.nodes, graph.edges, measureNode, measureLabel, "TB", aspect),
      layoutWorkflow(graph.nodes, graph.edges, measureNode, measureLabel, "LR", aspect),
    ]).then(([topToBottom, leftToRight]) => {
      if (cancelled) return;
      const chosen =
        preferred === "auto" ? chooseDirection(frame, topToBottom, leftToRight) : preferred;
      const laid = chosen === "LR" ? leftToRight : topToBottom;
      onLaidOut(chosen);
      laidBoundsRef.current = drawingBounds(laid);

      // The node-level handle sides change with the direction as well as the cards' own handles:
      // React Flow re-measures a node's handles when these two props change, in the node's own
      // render cycle, which is what keeps every wire on the sides the handles actually sit on.
      setNodes((current) =>
        current.map((flowNode) => {
          const box = laid.nodes.get(flowNode.id);
          const data = { ...flowNode.data, direction: chosen };
          const sides = handleSides(chosen);
          return box
            ? { ...flowNode, ...sides, data, position: { x: box.x, y: box.y } }
            : { ...flowNode, ...sides, data };
        }),
      );
      setEdges((current) =>
        current.map((flowEdge) => {
          const planned = laid.edges.find((e) => e.key === flowEdge.id);
          const from = laid.nodes.get(flowEdge.source);
          const to = laid.nodes.get(flowEdge.target);
          if (!planned || !from || !to || !flowEdge.data) return flowEdge;
          return {
            ...flowEdge,
            data: {
              ...flowEdge.data,
              points: planned.points,
              labelAt: planned.labelAt ?? null,
              arc: planned.arc ?? null,
              laidAt: { sx: from.x, sy: from.y, tx: to.x, ty: to.y },
              sweep: planned.sweep ?? false,
            },
          };
        }),
      );
      setLaidStamp((stamp) => stamp + 1);
    });
    return () => {
      cancelled = true;
      if (laidSignatureRef.current === layoutKey) laidSignatureRef.current = null;
    };
  }, [nodesInitialized, signature, preferred, relayoutStamp]);

  // Runs after the commit that put every handle on the side its wires now arrive at (the
  // orientation may have flipped), so the handles are re-measured where they really are and
  // every planned route is checked against true positions; the camera fits once that painted.
  // biome-ignore lint/correctness/useExhaustiveDependencies: once per layout, after it rendered
  useEffect(() => {
    if (laidStamp === 0) return;
    updateNodeInternals(graph.nodes.map((node) => node.key));
    const frame = requestAnimationFrame(() => {
      void fitDrawing();
    });
    return () => cancelAnimationFrame(frame);
  }, [laidStamp]);

  // The frame can change size after a layout has been fitted: a host resizes it once it learns
  // which way the ranks run, the window resizes, or the canvas goes full screen. Each time, the
  // camera fits the graph to the frame it actually has.
  useEffect(() => {
    const frame = frameRef.current;
    if (frame === null || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      void fitDrawing();
    });
    observer.observe(frame);
    return () => observer.disconnect();
  }, [fitDrawing]);

  return (
    <div ref={frameRef} className={cn("workflow-canvas h-full w-full", className)}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChangeChecked}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick ? (_, flowNode) => onNodeClick(flowNode.data.node) : undefined}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        minZoom={0.2}
        maxZoom={2.5}
        fitViewOptions={FIT}
        nodesConnectable={false}
        deleteKeyCode={null}
        // A dot matrix straight from a CSS radial-gradient, not React Flow's own SVG
        // Background — a couple of background-* properties, no extra pattern nodes.
        // Position/size track the live viewport so the dots pan and zoom with the
        // graph instead of sitting fixed to the screen.
        className="workflow-canvas-grid bg-ground"
        style={{
          backgroundImage: "radial-gradient(circle, var(--canvas-dot) 1.1px, transparent 1.6px)",
          backgroundPosition: `${viewX}px ${viewY}px`,
          backgroundSize: `${26 * zoom}px ${26 * zoom}px`,
        }}
      >
        {chrome === "overlay" ? (
          <>
            <Controls showInteractive={false} position="bottom-right" />
            <Panel position="top-left" className="!m-2">
              <OrientationToggle preferred={preferred} laidOut={laidOut} onChange={onPreferred} />
            </Panel>
            <Panel
              position="top-right"
              className="!m-2 rounded-full bg-surface/85 px-2.5 py-1 font-mono text-caption text-label-3"
            >
              {structuralFacts(graph)}
            </Panel>
            <Panel
              position="bottom-left"
              className="!m-2 rounded-full bg-surface/85 px-2.5 py-1 text-caption text-label-3"
            >
              Circle is a step, diamond is a gate a person owns, hexagon is a gate the agent owns.
            </Panel>
          </>
        ) : null}
      </ReactFlow>
    </div>
  );
}

// The mono fact string in the frame's own header strip — genuinely new
// information this canvas did not surface before, not a repeat of the run
// or workflow title the surrounding page already shows.
function structuralFacts(graph: WorkflowGraph): string {
  const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const nodeCount = graph.nodes.filter(
    (n) => n.kind === "run" || n.kind === "map" || n.kind === "judge",
  ).length;
  const gateCount = graph.nodes.filter(
    (n) => n.kind === "gate" || n.kind === "orchestrator-gate",
  ).length;
  const returnPathCount = graph.edges.filter((e) => e.kind === "rejected").length;
  return `${plural(nodeCount, "node")}, ${plural(gateCount, "gate")}, ${plural(returnPathCount, "return path")}`;
}

const ORIENTATIONS: { value: Orientation; word: string }[] = [
  { value: "auto", word: "Auto" },
  { value: "TB", word: "Down" },
  { value: "LR", word: "Across" },
];

// The orientation pills in the frame's header strip: Auto (the frame's own pick, named in
// parentheses once known) and the two directions by hand.
function OrientationToggle({
  preferred,
  laidOut,
  onChange,
}: {
  preferred: Orientation;
  laidOut: LayoutDirection;
  onChange: (value: Orientation) => void;
}): ReactElement {
  return (
    <fieldset className="m-0 flex items-center gap-1 border-0 p-0">
      <legend className="sr-only">Orientation</legend>
      {ORIENTATIONS.map(({ value, word }) => {
        const pressed = preferred === value;
        const label = value === "auto" ? `${word} (${laidOut === "LR" ? "across" : "down"})` : word;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={pressed}
            onClick={() => onChange(value)}
            className={cn(
              "rounded-full px-2.5 py-1 font-semibold text-caption transition-colors",
              pressed ? "bg-label-1 text-surface" : "text-label-2 hover:bg-well",
            )}
          >
            {label}
          </button>
        );
      })}
    </fieldset>
  );
}

export function WorkflowCanvas({
  graph,
  runState,
  className,
  onNodeClick,
  onOrientation,
  chrome = "overlay",
  preferred: preferredProp,
  onPreferredChange,
}: WorkflowCanvasProps) {
  const [own, setOwn] = useState<Orientation>("auto");
  const preferred = preferredProp ?? own;
  const setPreferred = (value: Orientation) => {
    setOwn(value);
    onPreferredChange?.(value);
  };
  const [laidOut, setLaidOut] = useState<LayoutDirection>("TB");
  if (graph.nodes.length === 0) {
    return (
      <div className={cn("flex items-center justify-center text-label-3 text-sm", className)}>
        Nothing to draw yet, pick a workflow on the right, or describe one in the conversation.
      </div>
    );
  }
  return (
    <div
      className={cn(
        "relative overflow-hidden bg-ground",
        chrome === "overlay" && "rounded-2xl border border-line-strong",
        className,
      )}
    >
      <ReactFlowProvider>
        <FlowInner
          graph={graph}
          runState={runState}
          onNodeClick={onNodeClick}
          preferred={preferred}
          chrome={chrome}
          laidOut={laidOut}
          onPreferred={setPreferred}
          onLaidOut={(laid) => {
            setLaidOut(laid);
            onOrientation?.(laid);
          }}
        />
      </ReactFlowProvider>
    </div>
  );
}
