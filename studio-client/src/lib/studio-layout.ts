import type { ELK, ElkExtendedEdge, ElkNode, LayoutOptions } from "elkjs/lib/elk-api";

import type { GraphEdge, GraphNode } from "@/api/studio-types";

// ELK's layered algorithm draws the workflow DAG: nodes ranked along the flow, crossings
// minimised, edges routed orthogonally around the cards it placed. Two things it does that
// the drawing depends on:
//
// Edge labels are LAYOUT OBJECTS with width and height, so the router reserves room for them
// and two labels physically cannot stack. Labels carry only the input NAME (the full
// `name: Type` goes to the tooltip), because a ~250px label on a rank-skipping edge acts as a
// fat invisible node and bends the whole layout around itself.
//
// Given the frame's aspect ratio, a long chain is WRAPPED: cut into rows (or columns) so the
// drawing fills the frame's shape instead of one thin line across it, the edge that wraps
// routed around the row it leaves. Without a ratio the ranks run in one line, which is what a
// frame with no size yet asks for.
//
// A rejection back edge never enters the layout. It closes a cycle the ranker would otherwise
// have to break by guessing which wire to reverse; its route is the bow `rejectionArc` computes
// from the laid card boxes instead, one that clears every card in its span by construction.

export interface NodeBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

// A rejection back-edge's own route: four vertices — source anchor, the two
// bends where it crosses to the bow line and back, target anchor — that bow
// out to one side of the node column instead of running through the cards
// ranked in between. Each of the two end segments moves away from its own
// card before it moves sideways, so the path never re-enters the box it just
// left; the middle segment runs at a fixed x already clear of every card in
// the span. `apex` is the midpoint of that middle segment — the bow's
// outer-most point, where the wiring label / rejection pip belongs, never
// mid-path where a plain label sits.
export interface RejectionArc {
  points: [Point, Point, Point, Point];
  apex: Point;
}

export interface LaidEdge {
  key: string;
  edge: GraphEdge;
  label: string;
  full: string;
  // The planned route, first point on the source's handle, last on the target's; the points
  // between are the bends of an orthogonal route (a rejection's are its bow's own vertices).
  points: { x: number; y: number }[];
  labelAt?: { x: number; y: number };
  arc?: RejectionArc;
  // A wrapped chain's carriage return: the target sits on the next row, left of its source.
  // The canvas draws it as a direct curve between the cards' facing borders, so `points`
  // holds only the two anchors, never a detour the drawing would have to make room for.
  sweep?: boolean;
}

export interface LaidGraph {
  width: number;
  height: number;
  nodes: Map<string, NodeBox>;
  edges: LaidEdge[];
}

export type MeasureNode = (node: GraphNode) => { width: number; height: number };
export type MeasureText = (text: string) => number;

// Which way the ranks run: top to bottom, or left to right.
export type LayoutDirection = "TB" | "LR";

// The zoom at which a laid graph fits a frame whole, the smaller of the two axes' ratios; 0 for
// a frame with no size yet (jsdom, a collapsed card), which never prefers one layout over another.
export function fitZoom(
  frame: { width: number; height: number },
  laid: { width: number; height: number },
): number {
  if (frame.width <= 0 || frame.height <= 0 || laid.width <= 0 || laid.height <= 0) return 0;
  return Math.min(frame.width / laid.width, frame.height / laid.height);
}

// The rule of thumb for orientation: lay the graph out both ways and keep the one the frame
// fits at the clearly larger zoom. Near-ties go left to right, because wrapped rows read like
// lines of text (across, then the next row down) while wrapped columns ask the eye to climb;
// top to bottom wins only when it fits meaningfully better, as on a tall frame. A frame with
// no size yet keeps top to bottom, the reading order of the spine.
export function chooseDirection(
  frame: { width: number; height: number },
  topToBottom: { width: number; height: number },
  leftToRight: { width: number; height: number },
): LayoutDirection {
  const across = fitZoom(frame, leftToRight);
  const down = fitZoom(frame, topToBottom);
  if (across === 0 && down === 0) return "TB";
  return across * 1.15 >= down ? "LR" : "TB";
}

// The width-to-height ratio a drawing should fill: the frame's own, or undefined for a frame
// with no size yet, which lays the ranks out in one line.
export function aspectRatioOf(frame: { width: number; height: number }): number | undefined {
  return frame.width > 0 && frame.height > 0 ? frame.width / frame.height : undefined;
}

function transposeBox(box: NodeBox): NodeBox {
  return { x: box.y, y: box.x, width: box.height, height: box.width };
}

function transposePoint(point: Point): Point {
  return { x: point.y, y: point.x };
}

const LABEL_HEIGHT = 13;

// How far past the right edge of every card in the arc's own vertical span the bow sits:
// enough daylight that the curve reads as routed around the column, never grazing it, and
// little enough that the bow and its label stay inside the air the camera leaves around the
// cards when it fits them.
const ARC_CLEARANCE = 24;

// How far each end segment reaches past its own card before turning toward
// the bow line — small, and well under a rank gap, but enough that the turn
// happens outside the card rather than exactly on its edge.
const ARC_STUB = 16;

// A rejection back-edge's route, computed from real card boxes rather than
// the layout's plan: every node whose vertical span overlaps the edge's own
// (siblings at the source's or target's rank, not only the two endpoints)
// sets how far right the bow has to reach to clear it. The curve leaves the
// source's bottom-center and enters the target's top-center — the same two
// points every other edge's handles sit at — so it reads as the same wire,
// just routed around instead of through.
//
// Each end segment is a straight line whose y only ever moves AWAY from the
// card it starts on (down from the source's bottom, up from the target's top),
// whichever of the two sits higher — a wrapped layout can put the target
// below the source — so it can never double back across that card's face,
// the danger a smooth curve through the same two points runs into, easing
// back toward the card's own x before it has climbed clear of the card's y
// range. The middle segment crosses at a fixed x already past every card in
// the span, so it carries no risk regardless of how tall that span is.
export function rejectionArc(
  nodes: Map<string, NodeBox>,
  sourceKey: string,
  targetKey: string,
  direction: LayoutDirection = "TB",
): RejectionArc | null {
  // Left to right is top to bottom with the axes swapped: compute the bow in the transposed
  // frame (where it clears the column to the right) and swap back (where it clears the row below).
  if (direction === "LR") {
    const transposed = new Map([...nodes].map(([key, box]) => [key, transposeBox(box)]));
    const arc = rejectionArc(transposed, sourceKey, targetKey, "TB");
    if (arc === null) return null;
    const [p0, p1, p2, p3] = arc.points;
    return {
      points: [transposePoint(p0), transposePoint(p1), transposePoint(p2), transposePoint(p3)],
      apex: transposePoint(arc.apex),
    };
  }
  const source = nodes.get(sourceKey);
  const target = nodes.get(targetKey);
  if (!source || !target) return null;

  const top = Math.min(source.y, target.y);
  const bottom = Math.max(source.y + source.height, target.y + target.height);

  // Endpoints in the same column take the short way: a hop out the column's left side,
  // spanning just these ranks, instead of a bow around everything the long side holds.
  // (Transposed for left to right, this is the hop over the top of a shared row.)
  if (source.x < target.x + target.width && target.x < source.x + source.width) {
    let leftmost = Math.min(source.x, target.x);
    for (const box of nodes.values()) {
      if (box.y + box.height <= top || box.y >= bottom) continue;
      leftmost = Math.min(leftmost, box.x);
    }
    const bowX = leftmost - ARC_CLEARANCE;
    const start: Point = { x: source.x, y: source.y + source.height / 2 };
    const end: Point = { x: target.x, y: target.y + target.height / 2 };
    const along = Math.sign(end.y - start.y) || -1;
    const nearSource: Point = { x: bowX, y: start.y + along * ARC_STUB };
    const nearTarget: Point = { x: bowX, y: end.y - along * ARC_STUB };
    return {
      points: [start, nearSource, nearTarget, end],
      apex: { x: bowX, y: (nearSource.y + nearTarget.y) / 2 },
    };
  }

  let rightmost = Math.max(source.x + source.width, target.x + target.width);
  for (const box of nodes.values()) {
    if (box.y + box.height <= top || box.y >= bottom) continue;
    rightmost = Math.max(rightmost, box.x + box.width);
  }
  const bowX = rightmost + ARC_CLEARANCE;

  const start: Point = { x: source.x + source.width / 2, y: source.y + source.height };
  const end: Point = { x: target.x + target.width / 2, y: target.y };
  const nearSource: Point = { x: bowX, y: start.y + ARC_STUB };
  const nearTarget: Point = { x: bowX, y: end.y - ARC_STUB };
  return {
    points: [start, nearSource, nearTarget, end],
    apex: { x: bowX, y: (nearSource.y + nearTarget.y) / 2 },
  };
}

// The gaps between cards and around wires. Cards carry an icon header plus up to three fact
// lines, so the gaps have to clear a taller, wider card: room for a wire's condition pip beside
// a rank boundary, and room for the rejection back-edge to bend into a visible bow instead of a
// straight line squeezed between two cards.
const SPACING: LayoutOptions = {
  "elk.spacing.nodeNode": "40",
  "elk.layered.spacing.nodeNodeBetweenLayers": "56",
  "elk.spacing.edgeNode": "20",
  "elk.layered.spacing.edgeNodeBetweenLayers": "24",
  "elk.spacing.edgeEdge": "12",
  "elk.spacing.edgeLabel": "4",
  // The channel a wrapped chain's wires travel between rows: without it the wire that carries
  // the spine to the next row hugs the cards it passes and reads as running underneath them.
  "elk.layered.wrapping.additionalEdgeSpacing": "48",
  "elk.padding": "[top=12,left=12,bottom=12,right=12]",
};

// One engine for the page, built on first use: the bundle is large and only a mounted canvas
// needs it, so it loads beside the canvas rather than with the app shell.
let engine: Promise<ELK> | undefined;

function elk(): Promise<ELK> {
  engine ??= import("elkjs/lib/elk.bundled.js").then(({ default: Engine }) => new Engine());
  return engine;
}

// A port's id in ELK's flat namespace: the node's key with the port's name appended, never
// parsed back apart.
function portId(key: string, port: "in" | "out"): string {
  return `${key} ${port}`;
}

// Where a card's wires arrive and leave: the same two points the canvas draws its handles at,
// so a planned route starts and ends exactly on a handle. Ports are fixed on the card at the
// side the direction implies; the router bends around from there.
function ports(
  key: string,
  direction: LayoutDirection,
  width: number,
  height: number,
): ElkNode["ports"] {
  const anchors =
    direction === "LR"
      ? { in: { x: 0, y: height / 2 }, out: { x: width, y: height / 2 } }
      : { in: { x: width / 2, y: 0 }, out: { x: width / 2, y: height } };
  // A port with no size is a point: the route starts and ends exactly on the anchor.
  return [
    { id: portId(key, "in"), width: 0, height: 0, x: anchors.in.x, y: anchors.in.y },
    { id: portId(key, "out"), width: 0, height: 0, x: anchors.out.x, y: anchors.out.y },
  ];
}

interface Route {
  points: Point[];
  labelAt?: Point;
}

// The one section a plain edge comes back with, as the route's points: start on the source
// handle, every bend, end on the target handle. A label's coordinates are its top-left corner;
// the canvas wants its centre.
function routeOf(laid: ElkExtendedEdge): Route {
  const section = laid.sections?.[0];
  const points: Point[] = section
    ? [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map((p) => ({
        x: p.x,
        y: p.y,
      }))
    : [];
  const label = laid.labels?.[0];
  const labelAt =
    label && label.x !== undefined && label.y !== undefined
      ? { x: label.x + (label.width ?? 0) / 2, y: label.y + (label.height ?? 0) / 2 }
      : undefined;
  return { points, labelAt };
}

// The wires a graph actually draws, each with its index in the graph's own edge order, so a
// planned route and a drawn edge always share `<from>-><to>#<index>` as their key.
//
// Two kinds of edge are left out. A wire whose end the graph does not draw, the same way the
// run order helpers ignore it. And a parameter's broadcast past its first consumer: an argument
// handed to four steps as four wires is most of a drawing's ink and none of its information --
// every consumer's signature already names the parameter -- so the parameter keeps one wire,
// to its first consumer, which anchors it in the drawing, and the rest are not wires at all.
export function drawnWires(
  nodes: GraphNode[],
  edges: GraphEdge[],
): { edge: GraphEdge; index: number }[] {
  const keys = new Set(nodes.map((node) => node.key));
  const params = new Set(nodes.filter((n) => n.kind === "param").map((n) => n.key));
  const broadcast = new Set<string>();
  const drawn: { edge: GraphEdge; index: number }[] = [];
  edges.forEach((edge, index) => {
    if (!keys.has(edge.from) || !keys.has(edge.to)) return;
    if (params.has(edge.from)) {
      if (broadcast.has(edge.from)) return;
      broadcast.add(edge.from);
    }
    drawn.push({ edge, index });
  });
  return drawn;
}

// Whether the drawn graph is a chain at heart: clearly deeper than it is wide, so wrapping
// harder only trades depth for rows. Depth and breadth come from the wires' own levels (a
// Kahn pass); a wire closing a cycle is ignored rather than looping the walk.
function chainlike(drawn: { edge: GraphEdge; index: number }[]): boolean {
  const nodes = new Set<string>();
  for (const { edge } of drawn) {
    nodes.add(edge.from);
    nodes.add(edge.to);
  }
  const level = new Map<string, number>();
  for (const key of nodes) level.set(key, 0);
  // Longest-path levels, relaxed |nodes| times at most: enough for any acyclic wiring.
  for (let pass = 0; pass < nodes.size; pass++) {
    let moved = false;
    for (const { edge } of drawn) {
      if (edge.kind === "rejected") continue;
      const next = (level.get(edge.from) ?? 0) + 1;
      if (next > (level.get(edge.to) ?? 0) && next < nodes.size) {
        level.set(edge.to, next);
        moved = true;
      }
    }
    if (!moved) break;
  }
  const perLevel = new Map<number, number>();
  for (const at of level.values()) perLevel.set(at, (perLevel.get(at) ?? 0) + 1);
  const depth = perLevel.size;
  const breadth = Math.max(1, ...perLevel.values());
  return depth >= 2 * breadth;
}

export async function layoutWorkflow(
  nodes: GraphNode[],
  edges: GraphEdge[],
  measureNode: MeasureNode,
  measureText: MeasureText,
  direction: LayoutDirection = "TB",
  aspectRatio?: number,
): Promise<LaidGraph> {
  const params = new Set(nodes.filter((n) => n.kind === "param").map((n) => n.key));

  const children: ElkNode[] = nodes.map((node) => {
    const { width, height } = measureNode(node);
    return {
      id: node.key,
      width,
      height,
      layoutOptions: { "elk.portConstraints": "FIXED_POS" },
      ports: ports(node.key, direction, width, height),
    };
  });

  // Rejections stay out of the router (they are drawn as the bow below).
  const drawn = drawnWires(nodes, edges);
  const routed: ElkExtendedEdge[] = [];
  const captions = new Map<number, { text: string; full: string }>();
  for (const { edge, index } of drawn) {
    const fromParam = params.has(edge.from);
    const full = fromParam ? "" : edge.label;
    const text = (full.split(":")[0] ?? "").trim();
    captions.set(index, { text, full });
    if (edge.kind === "rejected") continue;
    routed.push({
      id: String(index),
      sources: [portId(edge.from, "out")],
      targets: [portId(edge.to, "in")],
      labels: text ? [{ text, width: measureText(text), height: LABEL_HEIGHT }] : [],
      // A dataflow wire is the spine of the computation; a parameter's fan-out is a broadcast.
      // Straightening the spine first keeps the drawing a line of work, not a fan of arguments.
      layoutOptions: { "elk.layered.priority.straightness": fromParam ? "1" : "3" },
    });
  }

  const laid = await (await elk()).layout<ElkNode>({
    id: "workflow",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": direction === "LR" ? "RIGHT" : "DOWN",
      "elk.edgeRouting": "ORTHOGONAL",
      "elk.edgeLabels.placement": "CENTER",
      "elk.edgeLabels.inline": "true",
      // Wrapping is an across-only idea: rows read like lines of text, left to right and then
      // the next row down. Wrapped COLUMNS ask the eye to climb, scatter the reading order,
      // and put cards inside the span the rejection bow computes around -- so ranks down the
      // page mean the true spine, however tall, and the camera does the fitting.
      ...(direction === "LR" && aspectRatio !== undefined && aspectRatio > 0
        ? {
            "elk.aspectRatio": String(aspectRatio),
            "elk.layered.wrapping.strategy": "MULTI_EDGE",
            // The wrap estimator is systematically conservative: left at 1 it cuts a six-step
            // chain into rows of two inside a frame with room for rows of three. The factor
            // multiplies the ratio the estimator aims for -- but only a deep, narrow graph
            // gets the push: fed a shallow fork, the same push makes MULTI_EDGE split the
            // fork's siblings onto separate rows. (SINGLE_EDGE would protect forks by
            // construction, but elkjs 0.12 throws NoSuchElementException on it with fixed
            // ports.)
            "elk.layered.wrapping.correctionFactor": chainlike(drawn) ? "2.2" : "1.0",
          }
        : { "elk.layered.wrapping.strategy": "OFF" }),
      ...SPACING,
    },
    children,
    edges: routed,
  });

  const laidNodes = new Map<string, NodeBox>();
  for (const child of laid.children ?? []) {
    laidNodes.set(child.id, {
      x: child.x ?? 0,
      y: child.y ?? 0,
      width: child.width ?? 0,
      height: child.height ?? 0,
    });
  }

  const routes = new Map<number, Route>();
  for (const laidEdge of laid.edges ?? []) routes.set(Number(laidEdge.id), routeOf(laidEdge));

  const laidEdges: LaidEdge[] = [];
  for (const { edge, index } of drawn) {
    const caption = captions.get(index);
    if (!caption) continue;
    const key = `${edge.from}->${edge.to}#${index}`;
    if (edge.kind === "rejected") {
      const arc = rejectionArc(laidNodes, edge.from, edge.to, direction) ?? undefined;
      laidEdges.push({
        key,
        edge,
        label: caption.text,
        full: caption.full,
        points: arc?.points ?? [],
        arc,
      });
      continue;
    }
    const route = routes.get(index);
    const from = laidNodes.get(edge.from);
    const to = laidNodes.get(edge.to);
    // The carriage return of a wrapped chain (the target on the next row, left of its
    // source): its two anchors replace ELK's route around the whole drawing.
    if (
      direction === "LR" &&
      from &&
      to &&
      to.y > from.y + from.height &&
      to.x + to.width < from.x
    ) {
      const anchors = [
        { x: from.x + from.width, y: from.y + from.height / 2 },
        { x: to.x, y: to.y + to.height / 2 },
      ];
      laidEdges.push({
        key,
        edge,
        label: caption.text,
        full: caption.full,
        points: anchors,
        labelAt: {
          x: ((anchors[0]?.x ?? 0) + (anchors[1]?.x ?? 0)) / 2,
          y: ((anchors[0]?.y ?? 0) + (anchors[1]?.y ?? 0)) / 2,
        },
        sweep: true,
      });
      continue;
    }
    laidEdges.push({
      key,
      edge,
      label: caption.text,
      full: caption.full,
      points: route?.points ?? [],
      labelAt: caption.text ? route?.labelAt : undefined,
    });
  }

  return {
    width: Math.ceil(laid.width ?? 0),
    height: Math.ceil(laid.height ?? 0),
    nodes: laidNodes,
    edges: laidEdges,
  };
}
