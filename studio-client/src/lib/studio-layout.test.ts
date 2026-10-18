import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode } from "@/api/studio-types";

import {
  type LaidGraph,
  type NodeBox,
  type Point,
  aspectRatioOf,
  chooseDirection,
  drawnWires,
  layoutWorkflow,
  rejectionArc,
} from "./studio-layout";

// ELK lays the DAG out; this suite pins the properties the studio's readability depends on,
// not ELK's exact coordinates: ranks flow along the direction, siblings share a rank, labels
// are layout objects that reserve their own space (so they cannot stack), parameter fan-out is
// unlabeled, every route starts and ends on a handle and runs around the cards, and a long
// chain wraps to the frame's shape.

function node(key: string, kind: GraphNode["kind"] = "run"): GraphNode {
  return {
    key,
    id: key.replace(/^param:/, ""),
    kind,
    signature: `${key}(x: string) → Thing`,
    required: true,
    detail: "",
    tools: [],
    is_output: false,
    unknown: false,
    brought: false,
    briefing: "",
    input_schemas: {},
    output_schema: null,
    source: "",
  };
}

function wire(from: string, to: string, label = "", kind: GraphEdge["kind"] = "wire"): GraphEdge {
  return { from, to, kind, label };
}

const SIZE = { width: 180, height: 64 };
const measure = (text: string) => text.length * 6;

function overlaps(a: NodeBox, b: NodeBox): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

// Sample a polyline densely, so a route can be checked against every card box, not only at
// its vertices.
function samplePolyline(points: readonly Point[], stepsPerSegment = 20): Point[] {
  const out: Point[] = [];
  for (let seg = 0; seg < points.length - 1; seg++) {
    const from = points[seg];
    const to = points[seg + 1];
    if (!from || !to) continue;
    for (let i = 0; i <= stepsPerSegment; i++) {
      const t = i / stepsPerSegment;
      out.push({ x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t });
    }
  }
  return out;
}

// A hair inside the true edge, not on it — a route's own endpoints sit exactly on their node's
// boundary line by design, which is attachment, not intersection.
function insideBox(point: Point, box: NodeBox): boolean {
  return (
    point.x > box.x + 0.5 &&
    point.x < box.x + box.width - 0.5 &&
    point.y > box.y + 0.5 &&
    point.y < box.y + box.height - 0.5
  );
}

function expectNoOverlaps(laid: LaidGraph): void {
  const boxes = [...laid.nodes.values()];
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      if (!a || !b) continue;
      expect(overlaps(a, b)).toBe(false);
    }
  }
}

function expectRoutesClearOfCards(laid: LaidGraph): void {
  // A sweep edge's points are its two anchors, not a route: the canvas draws it as a curve
  // between the cards' facing borders, so there is no polyline to hold clear.
  for (const edge of laid.edges.filter((e) => !e.sweep)) {
    for (const point of samplePolyline(edge.points)) {
      for (const box of laid.nodes.values()) {
        expect(insideBox(point, box)).toBe(false);
      }
    }
  }
}

describe("layoutWorkflow", () => {
  it("puts a diamond's branches on one rank and its join below", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c"), node("d")],
      [wire("a", "b"), wire("a", "c"), wire("b", "d"), wire("c", "d")],
      () => SIZE,
      measure,
    );
    const at = (key: string) => {
      const box = laid.nodes.get(key);
      if (!box) throw new Error(`node ${key} was not laid out`);
      return box;
    };
    expect(at("b").y).toBeCloseTo(at("c").y); // siblings share the rank
    expect(at("a").y).toBeLessThan(at("b").y); // ranks flow downward
    expect(at("b").y).toBeLessThan(at("d").y);
    expect(at("b").x).not.toBeCloseTo(at("c").x); // and the fork actually forks
  });

  it("reserves label space on dataflow wires and anchors the label on its edge", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b")],
      [wire("a", "b", "prior_art: PriorArtFindings")],
      () => SIZE,
      measure,
    );
    const edge = laid.edges[0];
    expect(edge?.label).toBe("prior_art"); // name on the wire, type in the tooltip
    expect(edge?.full).toBe("prior_art: PriorArtFindings");
    expect(edge?.labelAt).toBeDefined();
    expect(edge?.points.length).toBeGreaterThanOrEqual(2);
  });

  it("leaves parameter fan-out unlabeled — the param card already says what it is", async () => {
    const laid = await layoutWorkflow(
      [node("param:context", "param"), node("a")],
      [wire("param:context", "a", "context: string")],
      () => SIZE,
      measure,
    );
    expect(laid.edges[0]?.label).toBe("");
    expect(laid.edges[0]?.labelAt).toBeUndefined();
  });

  it("keeps parallel edges apart as distinct laid edges", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b")],
      [wire("a", "b", "one: X"), wire("a", "b", "two: Y")],
      () => SIZE,
      measure,
    );
    expect(laid.edges).toHaveLength(2);
    expect(laid.edges[0]?.key).not.toBe(laid.edges[1]?.key);
  });

  it("reports a canvas size that contains every node box", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c")],
      [wire("a", "b"), wire("a", "c")],
      () => SIZE,
      measure,
    );
    for (const [, box] of laid.nodes) {
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.x + SIZE.width).toBeLessThanOrEqual(laid.width + 0.5);
      expect(box.y + SIZE.height).toBeLessThanOrEqual(laid.height + 0.5);
    }
  });

  it("starts every route on the source's handle and ends it on the target's", async () => {
    // The canvas draws the planned route only while both ends still sit on the handles it
    // measured; a route that started anywhere else would fall back to a live bezier at once.
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c")],
      [wire("a", "b", "x: Thing"), wire("a", "c", "y: Thing")],
      () => SIZE,
      measure,
      "LR",
    );
    for (const edge of laid.edges) {
      const from = laid.nodes.get(edge.edge.from);
      const to = laid.nodes.get(edge.edge.to);
      const first = edge.points[0];
      const last = edge.points[edge.points.length - 1];
      if (!from || !to || !first || !last) throw new Error("a route lost an end");
      expect(first.x).toBeCloseTo(from.x + from.width, 0);
      expect(first.y).toBeCloseTo(from.y + from.height / 2, 0);
      expect(last.x).toBeCloseTo(to.x, 0);
      expect(last.y).toBeCloseTo(to.y + to.height / 2, 0);
    }
  });

  // A gate's reviews wire (step -> gate) and its own rejected-to wire (gate -> step,
  // back to the same node) form a two-node cycle. The card footprint here is the
  // measured size of a real run/gate card with a header bar and its two-column
  // body grid. The param footprint is a trigger-style circle with its name and
  // type stacked below it — narrower than a step card, but taller than the old
  // single-line pill the spacing constants were tuned for.
  const CARD = { width: 300, height: 170 };
  const PARAM_CARD = { width: 96, height: 88 };

  function demoShape() {
    return layoutWorkflow(
      [node("param:input", "param"), node("step"), node("gate", "gate")],
      [
        wire("param:input", "step", "input: string"),
        wire("step", "gate", "reviews: Thing"),
        wire("gate", "step", "rejected", "rejected"),
      ],
      (n) => (n.kind === "param" ? PARAM_CARD : CARD),
      (text) => text.length * 6.5,
    );
  }

  it("keeps the gate below the step it reviews despite the rejected back-edge", async () => {
    // The back edge never reaches the ranker, so the cycle it closes can never be broken by
    // reversing the forward wire instead, which used to rank the gate above the step it reviews.
    const laid = await demoShape();
    const param = laid.nodes.get("param:input");
    const step = laid.nodes.get("step");
    const gate = laid.nodes.get("gate");
    if (!param || !step || !gate) throw new Error("demo shape did not lay out every node");
    expect(param.y).toBeLessThan(step.y);
    expect(step.y).toBeLessThan(gate.y);
  });

  it("never overlaps two card-sized boxes in the param/step/gate demo shape", async () => {
    expectNoOverlaps(await demoShape());
  });

  it("leaves enough rank clearance for a wire's condition pip and a visible back-edge curve", async () => {
    // The pip renders a few pixels off its node's connection point, and the
    // back-edge needs room to bend rather than reading as a line squeezed
    // between two cards — both need more clearance than one badge's height.
    const laid = await demoShape();
    const step = laid.nodes.get("step");
    const gate = laid.nodes.get("gate");
    if (!step || !gate) throw new Error("demo shape did not lay out every node");
    const clearance = gate.y - (step.y + step.height);
    expect(clearance).toBeGreaterThanOrEqual(40);
  });

  it("does not overlap card-sized siblings fanning out from one node", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c"), node("d")],
      [wire("a", "b"), wire("a", "c"), wire("a", "d")],
      () => CARD,
      (text) => text.length * 6.5,
    );
    expectNoOverlaps(laid);
  });

  it("routes every wire around the cards, never through one", async () => {
    // A rank-skipping wire (a -> d) has three cards between its ends; the route bends around
    // them rather than cutting across, which is what makes an arrow read as a wire.
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c"), node("d")],
      [
        wire("a", "b", "x: T"),
        wire("b", "c", "x: T"),
        wire("c", "d", "x: T"),
        wire("a", "d", "all: T"),
      ],
      () => CARD,
      (text) => text.length * 6.5,
    );
    expectRoutesClearOfCards(laid);
  });

  it("bows the rejection arc clear of every card in the param/step/gate column", async () => {
    const laid = await demoShape();
    const arc = rejectionArc(laid.nodes, "gate", "step");
    if (!arc) throw new Error("no arc computed for gate -> step");
    const samples = samplePolyline(arc.points);
    for (const box of laid.nodes.values()) {
      for (const point of samples) {
        expect(insideBox(point, box)).toBe(false);
      }
    }
  });

  it("bows the rejection arc clear of every card in a 4-node fan-out", async () => {
    // "a" fans out to three siblings; "c" rejects back up to "a" — the arc has
    // to clear "b" and "d" too, not only the two nodes it connects.
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c"), node("d")],
      [wire("a", "b"), wire("a", "c"), wire("a", "d"), wire("c", "a", "rejected", "rejected")],
      () => CARD,
      (text) => text.length * 6.5,
    );
    const arc = rejectionArc(laid.nodes, "c", "a");
    if (!arc) throw new Error("no arc computed for c -> a");
    const samples = samplePolyline(arc.points);
    for (const box of laid.nodes.values()) {
      for (const point of samples) {
        expect(insideBox(point, box)).toBe(false);
      }
    }
  });

  it("bows the rejection arc clear of the cards when the target sits below the source, as a wrapped layout can put it", () => {
    // Source in the first row, target in the second, a sibling beside the target: each end
    // segment still leaves its own card away from the card's face, so neither the source's
    // bottom nor the target's top is crossed, and the sibling is cleared like any other card.
    const boxes = new Map<string, NodeBox>([
      ["gate", { x: 400, y: 0, width: 300, height: 170 }],
      ["step", { x: 0, y: 400, width: 300, height: 170 }],
      ["sibling", { x: 400, y: 400, width: 300, height: 170 }],
    ]);
    const arc = rejectionArc(boxes, "gate", "step");
    if (!arc) throw new Error("no arc computed for gate -> step");
    for (const box of boxes.values()) {
      for (const point of samplePolyline(arc.points)) {
        expect(insideBox(point, box)).toBe(false);
      }
    }
    expect(arc.apex.x).toBeGreaterThan(700);
  });

  it("carries the arc on the laid edge itself, only for the rejected kind, as the edge's own route", async () => {
    const laid = await demoShape();
    const rejected = laid.edges.find((e) => e.edge.kind === "rejected");
    const forward = laid.edges.find((e) => e.edge.kind === "wire");
    expect(rejected?.arc).toBeDefined();
    expect(rejected?.points).toEqual(rejected?.arc?.points);
    expect(forward?.arc).toBeUndefined();
  });
});

describe("orientation", () => {
  const chain: GraphNode[] = [node("param:q", "param"), node("a"), node("b"), node("c"), node("d")];
  const chainWires: GraphEdge[] = [
    wire("param:q", "a", "q: string"),
    wire("a", "b", "x: Thing"),
    wire("b", "c", "x: Thing"),
    wire("c", "d", "x: Thing"),
  ];

  it("lays ranks out left to right when asked, every wire's target right of its source", async () => {
    const laid = await layoutWorkflow(
      chain,
      chainWires,
      () => SIZE,
      () => 40,
      "LR",
    );
    for (const edge of chainWires) {
      const from = laid.nodes.get(edge.from);
      const to = laid.nodes.get(edge.to);
      expect(from && to && to.x > from.x + from.width).toBe(true);
    }
  });

  it("prefers left to right for a deep chain in a wide frame, top to bottom in a tall one", async () => {
    const down = await layoutWorkflow(
      chain,
      chainWires,
      () => SIZE,
      () => 40,
      "TB",
    );
    const across = await layoutWorkflow(
      chain,
      chainWires,
      () => SIZE,
      () => 40,
      "LR",
    );
    expect(chooseDirection({ width: 1600, height: 500 }, down, across)).toBe("LR");
    expect(chooseDirection({ width: 500, height: 1600 }, down, across)).toBe("TB");
  });

  it("keeps top to bottom when the frame has no size yet", async () => {
    const down = await layoutWorkflow(
      chain,
      chainWires,
      () => SIZE,
      () => 40,
      "TB",
    );
    const across = await layoutWorkflow(
      chain,
      chainWires,
      () => SIZE,
      () => 40,
      "LR",
    );
    expect(chooseDirection({ width: 0, height: 0 }, down, across)).toBe("TB");
  });

  it("hops a left to right rejection over the top when source and target share the row", async () => {
    // The gate rejects to the step beside it: a short hop over the two cards, not a bow
    // around everything below.
    const nodes = [node("step"), node("gate", "gate"), node("after")];
    const wires = [
      wire("step", "gate", "reviews: Thing"),
      wire("gate", "step", "rejected", "rejected"),
      wire("gate", "after", "x: Thing"),
    ];
    const laid = await layoutWorkflow(
      nodes,
      wires,
      () => SIZE,
      () => 40,
      "LR",
    );
    const rejected = laid.edges.find((edge) => edge.edge.kind === "rejected");
    const arc = rejected?.arc;
    expect(arc).toBeDefined();
    if (!arc) return;
    const highest = Math.min(...[...laid.nodes.values()].map((box) => box.y));
    expect(arc.points[1].y).toBe(arc.points[2].y);
    expect(arc.apex.y).toBeLessThan(highest);
    // The hop leaves and enters the cards' top faces.
    const gate = laid.nodes.get("gate");
    const step = laid.nodes.get("step");
    expect(arc.points[0].y).toBe(gate?.y);
    expect(arc.points[3].y).toBe(step?.y);
  });

  it("keeps the bow around the drawing when the rejection target sits on another row", () => {
    const boxes = new Map<string, NodeBox>([
      ["gate", { x: 400, y: 0, width: 300, height: 170 }],
      ["step", { x: 0, y: 400, width: 300, height: 170 }],
    ]);
    const arc = rejectionArc(boxes, "gate", "step", "LR");
    if (!arc) throw new Error("no arc computed for gate -> step");
    expect(arc.apex.y).toBeGreaterThan(570);
  });

  it("draws a parameter's broadcast as one wire, to its first consumer only", async () => {
    // An argument handed to three steps as three wires is ink, not information: every
    // consumer's signature already names it. One wire anchors the parameter in the drawing.
    const nodes = [node("param:collection", "param"), node("a"), node("b"), node("c")];
    const edges = [
      wire("param:collection", "a", "collection: string"),
      wire("a", "b", "x: Thing"),
      wire("param:collection", "b", "collection: string"),
      wire("b", "c", "x: Thing"),
      wire("param:collection", "c", "collection: string"),
    ];
    expect(drawnWires(nodes, edges).map(({ index }) => index)).toEqual([0, 1, 3]);
    const laid = await layoutWorkflow(nodes, edges, () => SIZE, measure);
    expect(laid.edges.map((edge) => edge.edge.to)).toEqual(["a", "b", "c"]);
    expect(laid.edges.filter((edge) => edge.edge.from === "param:collection")).toHaveLength(1);
  });

  it("drops a wire whose end the graph does not draw instead of inventing a node for it", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b")],
      [wire("a", "b", "x: Thing"), wire("param:gone", "a", "gone: string")],
      () => SIZE,
      () => 40,
    );
    expect(laid.edges.map((edge) => edge.edge.from)).toEqual(["a"]);
  });
});

describe("wrapping to the frame", () => {
  const CARD = { width: 300, height: 170 };
  const seven = ["a", "b", "c", "d", "e", "f", "g"];
  const chain = seven.map((key) => node(key));
  const chainWires = seven
    .slice(1)
    .map((key, index) => wire(seven[index] ?? "", key, `${key}: Thing`));

  it("reads the frame's ratio, or none for a frame with no size yet", () => {
    expect(aspectRatioOf({ width: 2000, height: 1000 })).toBe(2);
    expect(aspectRatioOf({ width: 0, height: 0 })).toBeUndefined();
  });

  it("wraps a long chain into rows that fill a wide frame instead of one line across it", async () => {
    const line = await layoutWorkflow(chain, chainWires, () => CARD, measure, "LR");
    const wrapped = await layoutWorkflow(chain, chainWires, () => CARD, measure, "LR", 2);
    const rows = new Set([...wrapped.nodes.values()].map((box) => Math.round(box.y)));
    expect(rows.size).toBeGreaterThan(1);
    expect(Math.abs(wrapped.width / wrapped.height - 2)).toBeLessThan(
      Math.abs(line.width / line.height - 2),
    );
    expect(wrapped.nodes.size).toBe(seven.length);
    expectNoOverlaps(wrapped);
  });

  it("routes the wire that wraps around the row it leaves, and every other wire around the cards", async () => {
    const wrapped = await layoutWorkflow(chain, chainWires, () => CARD, measure, "LR", 2);
    expectRoutesClearOfCards(wrapped);
    for (const edge of wrapped.edges) {
      expect(edge.points.length).toBeGreaterThanOrEqual(2);
      expect(edge.labelAt).toBeDefined();
    }
  });

  it("marks the wire that wraps to the next row as a sweep with just its two anchors", async () => {
    // The carriage return of a wrapped chain: drawn as a direct curve through the row gap
    // rather than an orthogonal detour around the whole drawing.
    const wrapped = await layoutWorkflow(chain, chainWires, () => CARD, measure, "LR", 2);
    const sweeps = wrapped.edges.filter((edge) => edge.sweep);
    expect(sweeps.length).toBeGreaterThanOrEqual(1);
    for (const edge of sweeps) {
      const from = wrapped.nodes.get(edge.edge.from);
      const to = wrapped.nodes.get(edge.edge.to);
      if (!from || !to) throw new Error("sweep edge without laid boxes");
      expect(to.y).toBeGreaterThan(from.y + from.height);
      expect(to.x + to.width).toBeLessThan(from.x);
      expect(edge.points).toHaveLength(2);
      expect(edge.labelAt).toBeDefined();
    }
    const line = await layoutWorkflow(chain, chainWires, () => CARD, measure, "LR");
    expect(line.edges.every((edge) => !edge.sweep)).toBe(true);
  });

  it("keeps a wide fan-out to one line when it already fills the frame", async () => {
    const laid = await layoutWorkflow(
      [node("a"), node("b"), node("c"), node("d")],
      [wire("a", "b"), wire("a", "c"), wire("a", "d")],
      () => CARD,
      measure,
      "TB",
      2,
    );
    const ranks = new Set([...laid.nodes.values()].map((box) => Math.round(box.y)));
    expect(ranks.size).toBe(2);
  });
});
