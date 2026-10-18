"use client";

import { cn } from "client-core/lib/cn";

import type { GraphEdge, GraphNode, GraphResult } from "@/api/types";
import { nodeColor } from "@/lib/graph-model";

// The selection the canvas reports up: a node, an edge, or nothing yet.
export type GraphSelection = { node: GraphNode } | { edge: GraphEdge } | null;

// Renders the currently selected graph element:
//   - node  → title, type/status chips, id, and its NEIGHBOURS grouped by
//             relation, each one a button that walks the selection there
//   - edge  → src/dst titles (resolved against the loaded graph when possible,
//             else the raw id), rel, provenance_claim
//   - none  → an empty hint
//
// The neighbour list is the difference between describing the graph and
// reading it: a panel that only restates the five fields you already clicked
// is a dead end, and 343 nodes are not navigable by dragging alone.

interface GraphDetailPanelProps {
  selection: GraphSelection;
  graph: GraphResult;
  onSelectNode?: (id: string) => void;
  className?: string;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-[10px] text-label-3 uppercase tracking-wide">{label}</dt>
      <dd className="break-words text-[13.5px] text-label-1">{value}</dd>
    </div>
  );
}

// Every edge touching `id`, as (relation, the node at the other end, direction).
function neighboursOf(graph: GraphResult, id: string) {
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const found: { rel: string; other: string; title: string; outgoing: boolean }[] = [];
  for (const edge of graph.edges) {
    if (edge.src_id === id) {
      found.push({
        rel: edge.rel,
        other: edge.dst_id,
        title: byId.get(edge.dst_id)?.title ?? edge.dst_id,
        outgoing: true,
      });
    } else if (edge.dst_id === id) {
      found.push({
        rel: edge.rel,
        other: edge.src_id,
        title: byId.get(edge.src_id)?.title ?? edge.src_id,
        outgoing: false,
      });
    }
  }
  return found;
}

export function GraphDetailPanel({
  selection,
  graph,
  onSelectNode,
  className,
}: GraphDetailPanelProps) {
  if (selection === null) {
    return (
      <aside className={className} aria-label="Selection detail">
        <p className="text-[13.5px] text-label-3 leading-relaxed">
          Select a node or edge to see its details. Hover a node to isolate its neighbourhood;
          scroll to zoom, drag to pan.
        </p>
      </aside>
    );
  }

  if ("node" in selection) {
    const { id, type, title, status, superseded } = selection.node;
    const neighbours = neighboursOf(graph, id);
    return (
      <aside className={className} aria-label="Selection detail">
        <h2 className="font-semibold text-[14px] text-label-1 leading-snug">{title}</h2>
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          <span
            className="rounded-full px-2 py-0.5 text-[10px] text-white"
            style={{ backgroundColor: nodeColor(type) }}
          >
            {type}
          </span>
          <span className="rounded-full bg-muted-soft px-2 py-0.5 text-[10px] text-muted-ink">
            {status}
          </span>
          {superseded ? (
            <span className="rounded-full bg-warn-soft px-2 py-0.5 text-[10px] text-warn-ink">
              superseded
            </span>
          ) : null}
        </div>
        {/* The chips above ARE the type / status / superseded rendering; repeating
            them in the list would be two sources for one fact. */}
        <dl className="mt-3 flex flex-col gap-2">
          <Field label="ID" value={id} />
        </dl>

        <h3 className="mt-4 text-[10px] text-label-3 uppercase tracking-wide">
          Neighbours ({neighbours.length})
        </h3>
        {neighbours.length === 0 ? (
          <p className="mt-1 text-[13px] text-label-3">No edges, nothing links to this page.</p>
        ) : (
          <ul className="mt-1 space-y-0.5">
            {neighbours.map((neighbour) => (
              <li key={`${neighbour.rel}-${neighbour.other}-${neighbour.outgoing}`}>
                <button
                  type="button"
                  onClick={() => onSelectNode?.(neighbour.other)}
                  className="w-full rounded px-1 py-1 text-left hover:bg-well"
                >
                  <span className="font-mono text-[10px] text-label-3">
                    {neighbour.outgoing ? "→" : "←"} {neighbour.rel}
                  </span>
                  <span className="block truncate text-[13px] text-label-2">{neighbour.title}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>
    );
  }

  const { src_id, dst_id, rel, provenance_claim } = selection.edge;
  const titleFor = (nodeId: string): string =>
    graph.nodes.find((n) => n.id === nodeId)?.title ?? nodeId;

  return (
    <aside className={className} aria-label="Selection detail">
      <h2 className="font-semibold text-[14px] text-label-1">{rel}</h2>
      <dl className="mt-2 flex flex-col gap-2">
        <Field label="From" value={titleFor(src_id)} />
        <Field label="To" value={titleFor(dst_id)} />
        <Field label="Relation" value={rel} />
        <Field label="Provenance claim" value={provenance_claim ?? "none"} />
      </dl>
      <div className="mt-3 flex gap-1.5">
        {(
          [
            ["From", src_id],
            ["To", dst_id],
          ] as const
        ).map(([which, target]) => (
          <button
            key={which}
            type="button"
            onClick={() => onSelectNode?.(target)}
            className={cn(
              "rounded border border-line px-2 py-0.5 text-xs text-label-2",
              "hover:border-line-strong hover:bg-well",
            )}
          >
            open {which.toLowerCase()}
          </button>
        ))}
      </div>
    </aside>
  );
}
