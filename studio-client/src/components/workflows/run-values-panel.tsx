"use client";

import type { ReactElement } from "react";

import type { GraphNode } from "@/api/studio-types";

// A compact preview of what the run showing on the canvas actually produced, node by node, so
// reading any step's output no longer needs a click into a drawer first -- the click here only
// opens the same drawer for the FULL text, once the preview says it is worth it. Ordered by the
// graph's own node order, never by insertion order into `values`. Renders nothing while nothing
// has produced anything yet.

export interface RunValuesPanelProps {
  nodes: GraphNode[];
  values: Record<string, string> | null;
  onSelectNode: (nodeId: string) => void;
}

export function RunValuesPanel({
  nodes,
  values,
  onSelectNode,
}: RunValuesPanelProps): ReactElement | null {
  if (values === null || Object.keys(values).length === 0) return null;

  const order = new Map(nodes.map((node, index) => [node.id, index]));
  const entries = Object.entries(values).sort(
    (a, b) => (order.get(a[0]) ?? 0) - (order.get(b[0]) ?? 0),
  );

  return (
    <div className="space-y-1.5">
      <h3 className="font-semibold text-label-3 text-micro uppercase tracking-caps">Values</h3>
      <ul className="max-h-40 space-y-1 overflow-y-auto">
        {entries.map(([nodeId, value]) => (
          <li key={nodeId}>
            <button
              type="button"
              onClick={() => onSelectNode(nodeId)}
              className="block w-full rounded-md border border-line px-2.5 py-1.5 text-left transition-colors hover:border-line-strong hover:bg-well"
            >
              <span className="block font-mono text-caption text-label-2">{nodeId}</span>
              <span className="block truncate text-body text-label-1">{value}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
