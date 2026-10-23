import { MultiDirectedGraph } from "graphology";
import { describe, expect, it } from "vitest";

import { edgeLabelPlacement, labelledEdges } from "./edge-labels";

// The label layer draws on a 2D canvas, which jsdom cannot paint, so these tests pin the
// geometry it draws from. The canvas itself is covered by e2e/graph.spec.ts.

describe("edgeLabelPlacement", () => {
  it("turns the label of an edge drawn right to left the same way as left to right", () => {
    const left = { x: 100, y: 200, size: 5 };
    const right = { x: 300, y: 200, size: 5 };
    const forward = edgeLabelPlacement(left, right);
    const backward = edgeLabelPlacement(right, left);
    expect(forward?.angle).toBeCloseTo(0);
    expect(backward?.angle).toBeCloseTo(0);
  });

  it("keeps every direction within a quarter turn either way, so text never reads upside down", () => {
    const center = { x: 0, y: 0, size: 2 };
    for (let degrees = 0; degrees < 360; degrees += 15) {
      const radians = (degrees * Math.PI) / 180;
      const end = { x: 100 * Math.cos(radians), y: 100 * Math.sin(radians), size: 2 };
      const placement = edgeLabelPlacement(center, end);
      expect(placement).not.toBeNull();
      expect(Math.abs(placement?.angle ?? Number.NaN)).toBeLessThanOrEqual(Math.PI / 2 + 1e-9);
      // The text still lies along the edge: its direction is the edge's, or the edge's reversed.
      expect(Math.abs(Math.sin((placement?.angle ?? 0) - radians))).toBeCloseTo(0);
    }
  });

  it("centers the label on the stretch between the two node rims", () => {
    const placement = edgeLabelPlacement({ x: 0, y: 0, size: 10 }, { x: 100, y: 0, size: 30 });
    expect(placement?.x).toBeCloseTo(40);
    expect(placement?.y).toBeCloseTo(0);
    expect(placement?.length).toBeCloseTo(60);
  });

  it("places nothing when the two nodes overlap", () => {
    expect(edgeLabelPlacement({ x: 0, y: 0, size: 10 }, { x: 15, y: 0, size: 10 })).toBeNull();
  });
});

describe("labelledEdges", () => {
  // a - b - c, a - c, and d alone with an edge to c.
  function graph() {
    const g = new MultiDirectedGraph();
    for (const node of ["a", "b", "c", "d"]) g.addNode(node);
    g.addEdgeWithKey("ab", "a", "b");
    g.addEdgeWithKey("bc", "b", "c");
    g.addEdgeWithKey("ca", "c", "a");
    g.addEdgeWithKey("dc", "d", "c");
    return g;
  }

  it("lists an edge whose two nodes both show their labels, and no edge with one unlabelled end", () => {
    expect([...labelledEdges(graph(), new Set(["a", "b"]), null)]).toEqual(["ab"]);
  });

  it("lists every edge of the focused node, whatever its other end shows", () => {
    expect([...labelledEdges(graph(), new Set(), "c")].sort()).toEqual(["bc", "ca", "dc"]);
  });

  it("lists each edge once when its nodes are labelled and one is focused", () => {
    expect([...labelledEdges(graph(), new Set(["a", "b", "c"]), "a")].sort()).toEqual([
      "ab",
      "bc",
      "ca",
    ]);
  });

  it("ignores a focused or labelled node the graph no longer has", () => {
    expect([...labelledEdges(graph(), new Set(["a", "gone"]), "gone")]).toEqual([]);
  });
});
