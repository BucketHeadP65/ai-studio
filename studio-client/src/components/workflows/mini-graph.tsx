import { cn } from "client-core/lib/cn";
import { Fragment, type ReactElement } from "react";

import type { GraphEdge, GraphNode, WorkflowGraph } from "@/api/studio-types";
import { type GlyphFill, type GlyphKind, NodeGlyph } from "@/components/marks/node-glyph";

// The fleet card's own graph strip: one node group per real step or gate (a param carries no
// work, so it never earns a group here), left to right in the order the wiring runs. Kahn's
// algorithm over the workflow's own wire/map edges gives the same reading order the canvas's
// layout would, without pulling a layout engine onto a card this small; a rejection back edge never
// enters the ordering, since it would turn the pass into a cycle. `fillFor` is the one place a
// run's own progress reaches in -- this component only draws the shape and the run-relative ink.

export interface MiniGraphProps {
  graph: WorkflowGraph | undefined;
  fillFor: (node: GraphNode) => GlyphFill;
}

function glyphKindOf(kind: GraphNode["kind"]): GlyphKind {
  if (kind === "gate") return "person-gate";
  if (kind === "orchestrator-gate") return "agent-gate";
  return "step";
}

function topologicalOrder(nodes: GraphNode[], edges: GraphEdge[]): GraphNode[] {
  const keys = new Set(nodes.map((node) => node.key));
  const forward = edges.filter(
    (edge) =>
      (edge.kind === "wire" || edge.kind === "map") && keys.has(edge.from) && keys.has(edge.to),
  );
  const indegree = new Map<string, number>(nodes.map((node) => [node.key, 0]));
  const adjacency = new Map<string, string[]>(nodes.map((node) => [node.key, []]));
  for (const edge of forward) {
    indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1);
    adjacency.get(edge.from)?.push(edge.to);
  }
  const byKey = new Map(nodes.map((node) => [node.key, node]));
  const queue = nodes.filter((node) => indegree.get(node.key) === 0).map((node) => node.key);
  const seen = new Set<string>();
  const order: GraphNode[] = [];
  while (queue.length > 0) {
    const key = queue.shift();
    if (key === undefined || seen.has(key)) continue;
    seen.add(key);
    const node = byKey.get(key);
    if (node) order.push(node);
    for (const next of adjacency.get(key) ?? []) {
      const remaining = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, remaining);
      if (remaining === 0) queue.push(next);
    }
  }
  // A node only ever reached through a back edge (a rejection's own partner) never clears its
  // indegree the normal way; it still earns a group, appended in its original order rather than
  // dropped from the strip.
  for (const node of nodes) if (!seen.has(node.key)) order.push(node);
  return order;
}

const EDGE_CLASS = {
  active: "bg-accent",
  ran: "bg-label-1",
  faint: "bg-line",
} as const;

// Ink where both ends have run, cobalt where the run is entering the next node right now, faint
// where the wire has not been reached yet.
function edgeToneOf(fromFill: GlyphFill, toFill: GlyphFill): keyof typeof EDGE_CLASS {
  if (toFill === "running" || toFill === "waiting") return "active";
  if (fromFill !== "not-run" && toFill !== "not-run") return "ran";
  return "faint";
}

export function MiniGraph({ graph, fillFor }: MiniGraphProps): ReactElement | null {
  if (!graph) return null;
  const steps = graph.nodes.filter((node) => node.kind !== "param");
  if (steps.length === 0) return null;
  const ordered = topologicalOrder(steps, graph.edges);

  let pastFailure = false;
  const groups = ordered.map((node) => {
    const fill = fillFor(node);
    const faded = pastFailure;
    if (fill === "failed") pastFailure = true;
    return { node, fill, faded };
  });

  return (
    <div className="flex w-fit max-w-3xl items-start">
      {groups.map((group, index) => {
        const previous = groups[index - 1];
        return (
          <Fragment key={group.node.key}>
            {previous ? (
              <div
                aria-hidden
                data-edge-tone={edgeToneOf(previous.fill, group.fill)}
                className={cn(
                  "mt-3 h-0.5 min-w-4 flex-1 rounded-full",
                  EDGE_CLASS[edgeToneOf(previous.fill, group.fill)],
                  group.faded && "opacity-50",
                )}
              />
            ) : null}
            <div
              className={cn(
                "flex w-16 shrink-0 flex-col items-center gap-1.5",
                group.faded && "opacity-50",
              )}
            >
              <NodeGlyph kind={glyphKindOf(group.node.kind)} fill={group.fill} size={26} />
              <span className="w-full truncate text-center font-mono text-label-3 text-micro">
                {group.node.id}
              </span>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
