import { MultiDirectedGraph } from "graphology";
import { circular } from "graphology-layout";
import forceAtlas2 from "graphology-layout-forceatlas2";

import type { GraphResult } from "@/api/types";

// The Graph station's pure model: GraphResult -> a laid-out graphology graph.
// Kept free of sigma imports on purpose — sigma references WebGL at module
// load, which jsdom lacks, and everything testable about the canvas lives in
// this mapping anyway (colors, sizes, dimming, positions, the signature).

// -- typed-node color palette (unchanged vocabulary) ---------------------------

export const NODE_TYPE_COLORS = {
  paper: "#2563eb", // blue
  entity: "#16a34a", // green
  concept: "#9333ea", // purple
  comparison: "#ea580c", // orange
  playbook: "#0891b2", // cyan
  question: "#ca8a04", // amber
  map: "#db2777", // pink
  moc: "#db2777", // pink (backend alias of `map`)
  meta: "#64748b", // slate
  unknown: "#94a3b8", // slate-400 fallback
} as const;

export const FALLBACK_NODE_COLOR = NODE_TYPE_COLORS.unknown;
const SUPERSEDED_COLOR = "#cbd5e1";
const EDGE_COLOR = "#d7dee8";

export function nodeColor(type: string): string {
  return (NODE_TYPE_COLORS as Record<string, string>)[type] ?? FALLBACK_NODE_COLOR;
}

// Order-independent signature of the render-relevant content, so a
// keepPreviousData refetch that resolves to the same graph never triggers a
// rebuild + relayout (which would reshuffle the map under the reader).
export function graphSignature(graph: GraphResult): string {
  const nodes = graph.nodes
    .map((n) => `${n.id}|${n.title}|${n.type}|${n.status}|${n.superseded ? 1 : 0}`)
    .sort();
  const edges = graph.edges.map((e) => `${e.src_id}->${e.dst_id}->${e.rel}`).sort();
  return `${nodes.join("")}${edges.join("")}`;
}

// Live paper titles run to 120 characters, and sigma draws a label at full
// length regardless of the node's size — so an untruncated title lies across
// the canvas and over its neighbours' labels. The full string stays on the
// node as `title` for the detail panel and any tooltip.
const MAX_LABEL = 42;

function shortLabel(title: string): string {
  return title.length <= MAX_LABEL ? title : `${title.slice(0, MAX_LABEL - 3).trimEnd()}...`;
}

// GraphResult → a laid-out graphology MultiDirectedGraph. Pure: colors by type
// (superseded pages dimmed), sizes by degree (hubs read bigger), positions from
// a deterministic circular seed refined by ForceAtlas2.
export function toGraphology(graph: GraphResult): MultiDirectedGraph {
  const g = new MultiDirectedGraph();
  for (const node of graph.nodes) {
    g.addNode(node.id, {
      label: shortLabel(node.title),
      title: node.title,
      nodeType: node.type,
      superseded: node.superseded,
      color: node.superseded ? SUPERSEDED_COLOR : nodeColor(node.type),
      size: 3,
    });
  }
  graph.edges.forEach((edge, index) => {
    if (!g.hasNode(edge.src_id) || !g.hasNode(edge.dst_id)) return;
    g.addDirectedEdgeWithKey(
      `${edge.src_id}->${edge.dst_id}->${edge.rel}#${index}`,
      edge.src_id,
      edge.dst_id,
      {
        label: edge.rel,
        size: 1,
        color: EDGE_COLOR,
        type: "arrow",
      },
    );
  });
  g.forEachNode((node) => {
    g.setNodeAttribute(node, "size", Math.min(13, 3 + 1.6 * Math.sqrt(g.degree(node))));
  });
  if (g.order > 0) {
    circular.assign(g, { scale: 100 });
    // `strongGravityMode` is the setting that makes this knowledge base readable, and
    // it was chosen by measurement rather than taste. 106 of 487 live pages
    // have no edges at all; under ordinary gravity nothing pulls them off the
    // circular seed, so they hang in a wide ring around the graph and the
    // connected core — the part anyone came to read — gets fitted down to a
    // patch in the middle. Measured at the laptop viewport: the ring gave a
    // 509px core with 414 overlapping node pairs; this gives 581px and 14,
    // against a 631px ceiling if the unlinked pages were not drawn at all.
    // Drawing them matters: "94 unlinked entities" is a fact about the knowledge base,
    // not clutter to hide.
    forceAtlas2.assign(g, {
      iterations: 400,
      settings: {
        ...forceAtlas2.inferSettings(g),
        scalingRatio: 10,
        slowDown: 8,
        barnesHutOptimize: true,
        strongGravityMode: true,
        gravity: 0.6,
      },
    });
  }
  return g;
}
