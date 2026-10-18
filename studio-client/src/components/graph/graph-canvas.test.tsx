import { describe, expect, it } from "vitest";

import type { GraphResult } from "@/api/types";

import { FALLBACK_NODE_COLOR, graphSignature, nodeColor, toGraphology } from "@/lib/graph-model";

// sigma renders on WebGL, which jsdom does not have, so these tests pin the
// PURE mapping the canvas is built from: GraphResult → graphology attributes
// (color by type, size by degree, dimmed when superseded, positions assigned by
// the layout), plus the render-relevance signature. The WebGL mount itself is
// covered by the e2e smoke walk.

const GRAPH: GraphResult = {
  nodes: [
    { id: "paper:a", type: "paper", title: "Paper A", status: "verified", superseded: false },
    { id: "concept:b", type: "concept", title: "Concept B", status: "draft", superseded: false },
    { id: "paper:c", type: "paper", title: "Paper C", status: "draft", superseded: true },
  ],
  edges: [
    { src_id: "paper:a", dst_id: "concept:b", rel: "defines", provenance_claim: null },
    { src_id: "paper:a", dst_id: "paper:c", rel: "outperforms", provenance_claim: "c1" },
    { src_id: "paper:a", dst_id: "concept:b", rel: "cites", provenance_claim: null },
  ],
};

describe("toGraphology", () => {
  it("maps titles to labels, types to colors, and keeps every parallel edge", () => {
    const g = toGraphology(GRAPH);
    expect(g.order).toBe(3);
    expect(g.size).toBe(3); // parallel defines+cites both survive
    expect(g.getNodeAttribute("paper:a", "label")).toBe("Paper A");
    expect(g.getNodeAttribute("paper:a", "color")).toBe(nodeColor("paper"));
    expect(g.getNodeAttribute("concept:b", "color")).toBe(nodeColor("concept"));
  });

  it("sizes hubs above leaves and gives every node coordinates", () => {
    const g = toGraphology(GRAPH);
    const hub = g.getNodeAttribute("paper:a", "size") as number; // degree 3
    const leaf = g.getNodeAttribute("paper:c", "size") as number; // degree 1
    expect(hub).toBeGreaterThan(leaf);
    for (const node of g.nodes()) {
      expect(Number.isFinite(g.getNodeAttribute(node, "x"))).toBe(true);
      expect(Number.isFinite(g.getNodeAttribute(node, "y"))).toBe(true);
    }
  });

  it("dims a superseded node instead of painting it like a live one", () => {
    const g = toGraphology(GRAPH);
    expect(g.getNodeAttribute("paper:c", "color")).not.toBe(nodeColor("paper"));
  });

  it("skips an edge whose endpoint the payload never delivered, rather than throwing", () => {
    const g = toGraphology({
      nodes: GRAPH.nodes,
      edges: [
        ...GRAPH.edges,
        { src_id: "paper:a", dst_id: "ghost:x", rel: "cites", provenance_claim: null },
      ],
    });
    expect(g.size).toBe(3);
  });

  it("falls back to the unknown color for a type outside the vocabulary", () => {
    expect(nodeColor("no-such-type")).toBe(FALLBACK_NODE_COLOR);
  });

  // Paper titles run to 120 characters in the live knowledge base, and sigma draws a
  // label at full length whatever the node's size, so untruncated titles lie
  // across the canvas and over each other.
  it("truncates a long title for the canvas while keeping the full one readable", () => {
    const long =
      "S-Path-RAG: Semantic Aware Shortest-Path Retrieval Augmented Generation for Multi-hop QA";
    const g = toGraphology({
      nodes: [{ id: "p", type: "paper", title: long, status: "draft", superseded: false }],
      edges: [],
    });
    const label = g.getNodeAttribute("p", "label") as string;
    expect(label.length).toBeLessThan(long.length);
    expect(label.endsWith("...")).toBe(true);
    expect(g.getNodeAttribute("p", "title")).toBe(long);
  });
});

describe("graphSignature", () => {
  it("is order-independent and content-sensitive", () => {
    const reordered: GraphResult = {
      nodes: [...GRAPH.nodes].reverse(),
      edges: [...GRAPH.edges].reverse(),
    };
    expect(graphSignature(reordered)).toBe(graphSignature(GRAPH));
    const changed: GraphResult = {
      ...GRAPH,
      nodes: GRAPH.nodes.map((n) => (n.id === "paper:a" ? { ...n, title: "Renamed" } : n)),
    };
    expect(graphSignature(changed)).not.toBe(graphSignature(GRAPH));
  });
});
