"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import type { ReactElement } from "react";
import { useState } from "react";

import { runKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioApi } from "@/api/studio";
import type { RunEnvelope, RunIntent, WorkflowDetail } from "@/api/studio-types";
import { AdaptDialog } from "@/components/workflows/adapt-dialog";
import { BeforeRunDialog, type RunSetup } from "@/components/workflows/before-run-dialog";
import { ControlStrip } from "@/components/workflows/control-strip";
import { useDeliberateLaunch } from "@/components/workflows/use-launch";

// The interactive half of an open workflow: set a run up and start it, pause a
// run of it in flight, resume one that is paused, stop one for good, answer its
// gate, read back what the last run of it produced, and reuse it: run it again
// on the values a previous run was given, or make the next version out of one.
// Every act here is scoped to a session: `sessionId` names the session already
// running this workflow (found by the caller via `GET /api/runs`), or `null`
// when none is. A manual Run opens a fresh session of its own, titled after
// the workflow, and starts the run there — the session this component then
// keeps acting in until a page reload hands it a different one through the
// prop. The acting buttons themselves render through `ControlStrip`; this
// component owns their state and every mutation, plus the last-result readback
// beneath them.
//
// `run/state` carries no gate payload, so the gate a waiting run is waiting at
// is read from the same `runs()` poll that named the acting session, matched
// back to it by session id.
//
// A run stopped by a pause carries no flag of its own — the session's active
// run just goes idle — so "paused" is remembered here, set once a pause
// request answers, and cleared by a resume, a stop, or by starting a fresh
// run.
//
// A launch is set up before it exists: Run opens `BeforeRunDialog`, which collects the run's
// title, a typed value per declared parameter, and who decides this run's gates, and hands back
// one intent. Only then is a session created. The acts over a run already in flight -- pause,
// resume, stop, the gate decision, automode -- stay out here, where the run is.
//
// Starting is `useDeliberateLaunch`: one request per press, never retried, and a start whose
// answer never came back left as unknown for the person to settle by reading the runs. Run again
// is the same setup opened on a previous run's own values, and it starts nothing until it is
// confirmed like any other launch.
//
// Both reuse acts work from the run the reader has open, which the page names through
// `viewedRunId`: reopening that run's values, and citing that run as the reason a new version
// exists. With no run open, both fall back to the newest run that ended.

export interface RunControlsProps {
  detail: WorkflowDetail;
  sessionId: string | null;
  /**
   * The run the reader has open, which is the one both reuse acts work from.
   *
   * `null` is a reader who has picked none, and reuse then works from the newest run that ended.
   * A wrong value here lands in a durable record, since the adaptation writes down the run it
   * cites, so this is the run on the screen rather than the newest one the history happens to hold.
   */
  viewedRunId?: string | null;
  onSchedule?: () => void;
}

/** The newest ended run of this workflow: what reuse works from when the reader has none open. */
function newestEnded(runs: RunEnvelope[] | undefined): RunEnvelope | null {
  return runs?.find((run) => run.outcome !== "live") ?? null;
}

/** The ended run the reader has open, or `null` when none is open or the open one is still going. */
function openedRun(runs: RunEnvelope[] | undefined, runId: string | null): RunEnvelope | null {
  if (runId === null) return null;
  return runs?.find((run) => run.run_id === runId && run.outcome !== "live") ?? null;
}

/** A previous run's own setup, back as what a new preview opens holding. */
function setupOf(detail: WorkflowDetail, run: RunEnvelope): RunSetup {
  return { workflowId: detail.id, runTitle: run.run_title, arguments: run.params };
}

/**
 * What Run again opens on: the run the reader has open, else the setup this session last sent,
 * else the newest run that ended.
 */
function rerunSeed(
  detail: WorkflowDetail,
  opened: RunEnvelope | null,
  newest: RunEnvelope | null,
  lastIntent: RunIntent | null,
): RunSetup | null {
  if (opened !== null) return setupOf(detail, opened);
  if (lastIntent !== null) return lastIntent;
  return newest === null ? null : setupOf(detail, newest);
}

export function RunControls({
  detail,
  sessionId,
  viewedRunId = null,
  onSchedule,
}: RunControlsProps): ReactElement {
  const queryClient = useQueryClient();
  const [setupOpen, setSetupOpen] = useState(false);
  const [seed, setSeed] = useState<RunSetup | null>(null);
  const [adaptOpen, setAdaptOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [manualSessionId, setManualSessionId] = useState<string | null>(null);

  const activeSessionId = manualSessionId ?? sessionId;

  const runs = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const runState = useQuery({
    queryKey: runKeys.state(detail.id, activeSessionId),
    queryFn: () => sessionsApi.runState(activeSessionId ?? ""),
    enabled: activeSessionId !== null,
  });
  // The same key the inspector's own history poll uses, so reading a previous run's values here
  // costs nothing extra on the page that already holds them.
  const history = useQuery({
    queryKey: runKeys.history(detail.id),
    queryFn: () => studioApi.workflowHistory(detail.id),
  });
  // The run both reuse acts work from: the one open on the page, else the newest that ended.
  const source = openedRun(history.data, viewedRunId) ?? newestEnded(history.data);

  const invalidateRun = () =>
    queryClient.invalidateQueries({ queryKey: runKeys.state(detail.id, activeSessionId) });

  const launch = useDeliberateLaunch({
    sessionTitle: detail.title || detail.id,
    onStarted: (startedIn) => {
      setSetupOpen(false);
      setManualSessionId(startedIn);
      setPaused(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
    },
    onReconciled: (found) => {
      setSetupOpen(false);
      setManualSessionId(found.sessionId);
      setPaused(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      queryClient.invalidateQueries({ queryKey: runKeys.history(detail.id) });
    },
  });

  function openSetup(from: RunSetup | null): void {
    launch.reset();
    setSeed(from);
    setSetupOpen(true);
  }

  const pause = useMutation({
    mutationFn: () => sessionsApi.pauseRun(activeSessionId ?? ""),
    onSuccess: () => {
      setPaused(true);
      invalidateRun();
    },
  });

  const resume = useMutation({
    mutationFn: () => sessionsApi.resumeRun(activeSessionId ?? ""),
    onSuccess: () => {
      setPaused(false);
      invalidateRun();
    },
  });

  const stop = useMutation({
    mutationFn: () => sessionsApi.stopRun(activeSessionId ?? ""),
    onSuccess: () => {
      setPaused(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      invalidateRun();
    },
  });

  const decide = useMutation({
    mutationFn: ({ approve, feedback }: { approve: boolean; feedback?: string }) =>
      sessionsApi.decide(activeSessionId ?? "", approve, feedback),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      invalidateRun();
    },
  });

  const automode = useMutation({
    mutationFn: (on: boolean) => sessionsApi.setAutomode(activeSessionId ?? "", on),
    onSuccess: invalidateRun,
  });

  const activeRun = runs.data?.find((view) => view.session_id === activeSessionId) ?? null;
  const gate = activeRun?.state === "waiting" ? activeRun.gate : null;

  const running = runState.data?.running === true;
  const result = runState.data?.result ?? null;
  // The poll is the source of truth -- an optimistic flip only bridges the round trip while
  // the mutation itself is still in flight, so a reload or a second viewer always shows the
  // backend's own flag rather than a value only this component ever set.
  const automodeOn = automode.isPending
    ? (automode.variables ?? false)
    : (runState.data?.auto_approve ?? false);

  const rerunFrom = rerunSeed(
    detail,
    openedRun(history.data, viewedRunId),
    newestEnded(history.data),
    launch.lastIntent,
  );
  return (
    <div className="space-y-2.5">
      <ControlStrip
        gate={gate}
        onDecide={(approve, feedback) => decide.mutate({ approve, feedback })}
        deciding={decide.isPending}
        running={running}
        paused={paused}
        onRun={() => openSetup(null)}
        runDisabled={launch.starting || running || !detail.fits}
        starting={launch.starting}
        onPause={() => pause.mutate()}
        pausing={pause.isPending}
        onResume={() => resume.mutate()}
        resuming={resume.isPending}
        onStop={() => stop.mutate()}
        stopping={stop.isPending}
        onSchedule={onSchedule}
        automode={automodeOn}
        onAutomode={(on) => automode.mutate(on)}
        settingAutomode={automode.isPending}
        gateMode={runState.data?.gate_mode ?? "wait"}
      />

      {rerunFrom === null && source === null ? null : (
        <div className="flex flex-wrap gap-2">
          {rerunFrom === null ? null : (
            <Button
              variant="secondary"
              onClick={() => openSetup(rerunFrom)}
              disabled={launch.starting || running || !detail.fits}
              className="h-7 px-3 text-caption"
            >
              Run again
            </Button>
          )}
          <Button
            variant="secondary"
            onClick={() => setAdaptOpen(true)}
            className="h-7 px-3 text-caption"
          >
            Make a new version
          </Button>
        </div>
      )}

      <BeforeRunDialog
        open={setupOpen}
        workflowId={detail.id}
        workflowTitle={detail.title || detail.id}
        detail={detail}
        submitting={launch.starting}
        error={launch.error}
        seed={seed}
        unknownReason={launch.unknownReason}
        onReconcile={launch.reconcile}
        reconciling={launch.reconciling}
        settledAsNothing={launch.settledAsNothing}
        onConfirm={(intent) => launch.start(intent)}
        onCancel={() => setSetupOpen(false)}
      />

      {/* Mounted only once it is asked for: it navigates to the new version's draft, and a
          router belongs to the act rather than to the strip of buttons offering it. */}
      {adaptOpen ? (
        <AdaptDialog
          open
          workflowId={detail.id}
          description={detail.description}
          sourceRunId={source?.run_id ?? ""}
          sourceRunTitle={source?.run_title ?? ""}
          onCancel={() => setAdaptOpen(false)}
        />
      ) : null}

      {result ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-ok-ink/30 bg-ok-soft/50 p-2.5 font-mono text-caption text-label-2">
          {result}
        </pre>
      ) : null}
    </div>
  );
}
