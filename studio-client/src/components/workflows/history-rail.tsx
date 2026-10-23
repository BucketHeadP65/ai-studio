"use client";

import { cn } from "@/shared/lib/cn";
import { formatRelative } from "@/shared/lib/format-relative";
import { Star, Trash2 } from "lucide-react";
import type { ReactElement } from "react";

import type { RunEnvelope } from "@/api/studio-types";
import { RUN_STATE_PILL, type RunStatePill } from "@/components/marks/run-state";
import { StatePill } from "@/components/marks/state-pill";

// Every run this workflow has had, newest first, exactly as `workflowHistory`
// answers it -- the backend already prepends a "live" entry while one is in
// flight, so this component never sorts or synthesizes rows of its own. The
// live row (if present) carries no persisted record yet, so it gets neither a
// pin nor a delete. Selection is a run id, or `null` naming the live row --
// the same `null` a caller uses when there is no live row at all, in which
// case nothing here renders as selected. A `StatePill` names each past run's
// outcome; the live row keeps its own "Live" label instead, since it has no
// outcome yet to name.

export interface HistoryRailProps {
  runs: RunEnvelope[];
  selectedRunId: string | null;
  onSelect: (run: RunEnvelope) => void;
  onTogglePin: (run: RunEnvelope) => void;
  pinningId: string | null;
  onDeleteRequest: (run: RunEnvelope) => void;
}

const OUTCOME_PILL: Record<Exclude<RunEnvelope["outcome"], "live">, RunStatePill> = {
  completed: RUN_STATE_PILL.completed,
  failed: RUN_STATE_PILL.failed,
  stopped: RUN_STATE_PILL.stopped,
};

function isSelected(run: RunEnvelope, selectedRunId: string | null): boolean {
  return run.outcome === "live" ? selectedRunId === null : selectedRunId === run.run_id;
}

// Only ever called for a past run -- the live row names itself "Live" at the
// call site and never reaches here.
function whenLabel(run: RunEnvelope): string {
  return run.ended_at !== "" ? formatRelative(run.ended_at) : formatRelative(run.started_at);
}

export function HistoryRail({
  runs,
  selectedRunId,
  onSelect,
  onTogglePin,
  pinningId,
  onDeleteRequest,
}: HistoryRailProps): ReactElement | null {
  if (runs.length === 0) return null;

  return (
    <div className="space-y-1.5">
      <h3 className="font-semibold text-label-3 text-micro uppercase tracking-caps">History</h3>
      <ul className="space-y-1.5">
        {runs.map((run) => {
          const selected = isSelected(run, selectedRunId);
          return (
            <li
              key={run.run_id}
              className={cn(
                "flex items-center gap-1.5 rounded-md border px-2.5 py-2",
                selected ? "border-accent bg-accent-soft/40" : "border-line bg-surface",
              )}
            >
              <button
                type="button"
                onClick={() => onSelect(run)}
                aria-pressed={selected}
                className="flex min-w-0 flex-1 items-center gap-2 text-left"
              >
                <span className="min-w-0 flex-1 truncate text-body text-label-1">
                  {run.outcome === "live" ? "Live" : whenLabel(run)}
                </span>
                {run.outcome !== "live" ? (
                  <StatePill tone={OUTCOME_PILL[run.outcome].tone} size="sm">
                    {OUTCOME_PILL[run.outcome].word}
                  </StatePill>
                ) : null}
              </button>
              {run.outcome !== "live" ? (
                <>
                  <button
                    type="button"
                    aria-label={run.pinned ? "Unpin this run" : "Pin this run"}
                    aria-pressed={run.pinned}
                    onClick={() => onTogglePin(run)}
                    disabled={pinningId === run.run_id}
                    className="shrink-0 rounded p-1 text-label-2 hover:bg-well disabled:opacity-50"
                  >
                    <Star size={13} fill={run.pinned ? "currentColor" : "none"} />
                  </button>
                  <button
                    type="button"
                    aria-label="Delete this run"
                    onClick={() => onDeleteRequest(run)}
                    className="shrink-0 rounded p-1 text-label-2 hover:bg-danger-soft hover:text-danger-ink"
                  >
                    <Trash2 size={13} />
                  </button>
                </>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
