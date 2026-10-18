"use client";

import { ChevronLeft } from "lucide-react";
import { useRouter } from "next/navigation";
import type { ReactElement } from "react";

import type { GraphNode, WorkflowGraph } from "@/api/studio-types";
import type { RunHighlight } from "@/components/workflows/workflow-canvas";
import { WorkflowCanvas } from "@/components/workflows/workflow-canvas";

// The full-page shell one workflow gets: a title bar above with the way back
// to wherever the reader came from, and a side column that differs by who
// called it — the run controls for a stored workflow, the draft column for
// one still in the authoring conversation. The main column below the title
// bar takes one of two shapes: the plain canvas-hero every caller but the
// run detail page still uses (`graph` fills the column, an optional
// `footer` sits beneath it, `onNodeClick` opens a node), or `body`, the run
// detail page's own stacked cards (the outcome strip, the gate trail, the
// timeline card, the canvas card), which replaces the hero entirely when
// given since only that caller knows which run is in view. Either shape
// stacks above the side column instead of sitting beside it once the
// viewport is too narrow for both, so the page reflows rather than ever
// scrolling horizontally.
//
// `header`, when a caller has one, replaces the plain title with its own
// richer block (identity, edit, the run actions) — `title` still names the
// page for a caller with nothing richer to show, and stays required so a
// draft page's plain bar is unaffected.

export interface InspectorPageProps {
  title: string;
  header?: ReactElement;
  side: ReactElement;
  graph?: WorkflowGraph;
  highlight?: RunHighlight;
  onNodeClick?: (node: GraphNode) => void;
  footer?: ReactElement | null;
  body?: ReactElement;
}

export function InspectorPage({
  title,
  header,
  graph,
  highlight,
  side,
  onNodeClick,
  footer,
  body,
}: InspectorPageProps): ReactElement {
  const router = useRouter();
  return (
    <div className="-m-6 flex h-[calc(100%+3rem)] flex-col">
      <div className="flex items-start gap-3 border-line border-b bg-surface px-4 py-3">
        <button
          type="button"
          onClick={() => router.back()}
          className="flex shrink-0 items-center gap-1 rounded-md border border-line px-2 py-1.5 text-caption text-label-2 transition-colors hover:border-line-strong hover:bg-well hover:text-label-1"
        >
          <ChevronLeft size={14} />
          Back
        </button>
        {header ?? <h1 className="truncate font-semibold text-label-1 text-title">{title}</h1>}
      </div>
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {body ? (
          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto px-7 pt-5 pb-7">
            {body}
          </div>
        ) : (
          <div className="flex min-h-[360px] min-w-0 flex-1 flex-col lg:min-h-0">
            <div className="relative min-h-0 flex-1 bg-ground">
              {graph ? (
                <WorkflowCanvas
                  graph={graph}
                  runState={highlight}
                  onNodeClick={onNodeClick}
                  className="absolute inset-0"
                />
              ) : null}
            </div>
            {footer}
          </div>
        )}
        <aside className="max-h-[50vh] w-full shrink-0 overflow-y-auto border-line border-t bg-surface p-4 lg:h-auto lg:max-h-none lg:w-96 lg:border-t-0 lg:border-l">
          {side}
        </aside>
      </div>
    </div>
  );
}
