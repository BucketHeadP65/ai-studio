"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { Play } from "lucide-react";
import { useRouter } from "next/navigation";
import { type ReactElement, useMemo } from "react";

import { runKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import type { RunView } from "@/api/sessions-types";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { RunEnvelope } from "@/api/studio-types";
import { useNowTick } from "@/components/workflows/elapsed";
import { type FleetCard, RunCard } from "@/components/workflows/run-card";
import { RunLegend } from "@/components/workflows/run-legend";
import { runName } from "@/lib/run-name";

// The fleet at /workflows: one vertical stack of every run worth watching, waiting first, then
// running, then paused, then the archive newest first -- no lanes, and no scheduled runs (their
// own station on the sidebar owns that now). One board-wide clock ticks every live card's elapsed
// time; a run's workflow graph is fetched once per distinct id on the board and shared by every
// card that needs it.
//
// A card is named by the run itself: the title its launch gave it, the workflow's own card title
// for a run nobody named, and the workflow id when neither exists. A session's name is never any
// of those -- one session can host several runs, and naming a run after its container tells a
// reader which box it is in rather than what it was started to do.

const RECENT_LIMIT = 20;
const GRAPH_STALE_TIME = 5 * 60 * 1000;

function isToday(iso: string): boolean {
  if (iso === "") return false;
  const at = new Date(iso);
  const now = new Date();
  return (
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate()
  );
}

function summarize(waiting: number, running: number, finishedToday: number): string {
  const clauses: string[] = [];
  if (waiting > 0) clauses.push(`${waiting} waiting on you`);
  if (running > 0) clauses.push(`${running} running`);
  if (finishedToday > 0) clauses.push(`${finishedToday} finished today`);
  return clauses.length === 0 ? "Nothing running." : `${clauses.join(", ")}.`;
}

export default function WorkflowsPage(): ReactElement {
  const router = useRouter();
  const queryClient = useQueryClient();
  const now = useNowTick();

  const runsQuery = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const recentQuery = useQuery({
    queryKey: runKeys.recent,
    queryFn: () => studioApi.recentRuns(RECENT_LIMIT),
  });
  const decide = useMutation({
    mutationFn: ({
      sessionId,
      approve,
      feedback,
    }: {
      sessionId: string;
      approve: boolean;
      feedback?: string;
    }) => sessionsApi.decide(sessionId, approve, feedback),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.live }),
  });
  const resume = useMutation({
    mutationFn: (sessionId: string) => sessionsApi.resumeRun(sessionId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.live }),
  });
  const pin = useMutation({
    mutationFn: ({ runId, pinned }: { runId: string; pinned: boolean }) =>
      studioApi.pinRun(runId, pinned),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.recent }),
  });

  const runs: RunView[] = runsQuery.data ?? [];
  const waiting = runs.filter((run) => run.state === "waiting");
  const running = runs.filter((run) => run.state === "running");
  const paused = runs.filter((run) => run.state === "paused");
  const recent: RunEnvelope[] = recentQuery.data ?? [];

  const workflowIds = useMemo(() => {
    const ids = new Set<string>();
    for (const run of runs) ids.add(run.workflow_id);
    for (const run of recent) ids.add(run.workflow_id);
    return Array.from(ids);
  }, [runs, recent]);

  const graphQueries = useQueries({
    queries: workflowIds.map((id) => ({
      queryKey: ["studio", "workflow", id],
      queryFn: () => studioApi.workflow(id),
      staleTime: GRAPH_STALE_TIME,
    })),
  });
  const cardById = new Map(workflowIds.map((id, index) => [id, graphQueries[index]?.data]));
  const graphById = new Map(workflowIds.map((id) => [id, cardById.get(id)?.graph]));

  const finishedToday = recent.filter((run) => isToday(run.ended_at)).length;
  const summary = summarize(waiting.length, running.length, finishedToday);

  const errorMessage = runsQuery.isError
    ? studioErrorMessage(runsQuery.error)
    : recentQuery.isError
      ? studioErrorMessage(recentQuery.error)
      : null;

  const cards: FleetCard[] = [
    ...waiting.map(
      (run): FleetCard => ({
        kind: "waiting",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => cardById.get(id)?.title),
        graph: graphById.get(run.workflow_id),
        now,
        onDecide: (approve, feedback) =>
          decide.mutate({ sessionId: run.session_id, approve, feedback }),
        deciding: decide.isPending && decide.variables?.sessionId === run.session_id,
      }),
    ),
    ...running.map(
      (run): FleetCard => ({
        kind: "running",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => cardById.get(id)?.title),
        graph: graphById.get(run.workflow_id),
        now,
      }),
    ),
    ...paused.map(
      (run): FleetCard => ({
        kind: "paused",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => cardById.get(id)?.title),
        graph: graphById.get(run.workflow_id),
        now,
        onResume: () => resume.mutate(run.session_id),
        resuming: resume.isPending && resume.variables === run.session_id,
      }),
    ),
    ...recent.map(
      (run): FleetCard => ({
        kind: "recent",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => cardById.get(id)?.title),
        graph: graphById.get(run.workflow_id),
        onTogglePin: () => pin.mutate({ runId: run.run_id, pinned: !run.pinned }),
        pinning: pin.isPending && pin.variables?.runId === run.run_id,
      }),
    ),
  ];

  function cardKey(card: FleetCard): string {
    return card.kind === "recent" ? card.run.run_id : card.run.session_id;
  }

  return (
    <div className="-m-6 flex flex-col">
      <div className="flex min-h-[70px] flex-wrap items-center gap-x-3 gap-y-2 border-line border-b bg-surface px-4 py-3 sm:px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Fleet</h1>
          <p className="text-caption text-label-3">{summary}</p>
        </div>
        <Button
          onClick={() => router.push("/workflows/library")}
          className="ml-auto inline-flex shrink-0 items-center gap-1.5"
        >
          <Play size={14} fill="currentColor" aria-hidden="true" />
          Start a run
        </Button>
      </div>

      <div className="flex flex-col gap-4 px-4 pt-5 pb-7 sm:px-7">
        {errorMessage ? (
          <p
            role="alert"
            className="rounded-md border border-warn/40 bg-danger-soft px-3 py-2 text-caption text-danger-ink"
          >
            Could not refresh the fleet. {errorMessage}
          </p>
        ) : null}

        {cards.length === 0 ? (
          <p className="text-caption text-label-3">No runs to show yet.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {cards.map((card) => (
              <RunCard key={cardKey(card)} card={card} />
            ))}
          </ul>
        )}

        <RunLegend />
      </div>
    </div>
  );
}
