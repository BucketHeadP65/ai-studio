"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { type ReactElement, type Ref, useState } from "react";

import { harnessApi } from "@/api/harness";
import { runKeys, sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { GateMode } from "@/api/studio-types";
import { ClearControl, VitalsReadout } from "@/components/follow-me/vitals";
import { GATE_MODE_WORDS } from "@/components/marks/gate-mode";
import { useNowTick } from "@/components/workflows/elapsed";
import { cn } from "@/shared/lib/cn";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

// The chat station's one status row, under the composer: this session's own cost and context
// reading, the choice of who decides a gate this run reaches, how many plugins the agent may use
// here, the live run's pause and stop, and the control to start the session over. Everything the
// old right rail carried in a rail's worth of room, on one line -- the live run's own summary is
// not repeated here, the run turn in the transcript already shows it.

const GATE_MODES: readonly GateMode[] = ["wait", "orchestrate", "auto"];

export interface SessionStatuslineProps {
  sessionId: string | null;
  gateMode: GateMode;
  onGateModeChange: (mode: GateMode) => void;
  // Called after Clear forgets this session's conversation, so the caller can remount the
  // conversation it mounts elsewhere on the page.
  onCleared: () => void;
  firstGateRadioRef?: Ref<HTMLInputElement>;
  // ISO instant the viewed session's in-flight turn began, owned by the chat station above
  // both this row and the conversation. `null` while no turn is running.
  turnStartedAt?: string | null;
}

export function SessionStatusline({
  sessionId,
  gateMode,
  onGateModeChange,
  onCleared,
  firstGateRadioRef,
  turnStartedAt = null,
}: SessionStatuslineProps): ReactElement {
  const queryClient = useQueryClient();
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

  const detail = useQuery({
    queryKey: sessionKeys.detail(sessionId),
    queryFn: () => sessionsApi.detail(sessionId ?? ""),
    enabled: sessionId !== null,
  });

  const runs = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const run = runs.data?.find((candidate) => candidate.session_id === sessionId) ?? null;
  const turnNow = useNowTick(1000, turnStartedAt !== null);

  const runState = useQuery({
    queryKey: runKeys.state(run?.workflow_id ?? "", sessionId),
    queryFn: () => sessionsApi.runState(sessionId ?? ""),
    enabled: run !== null,
  });

  const plugins = useQuery({
    queryKey: ["harness", "plugins"],
    queryFn: () => harnessApi.plugins(),
  });
  const openPluginCount = plugins.data?.plugins.filter((plugin) => plugin.enabled).length ?? 0;

  function invalidateRun() {
    queryClient.invalidateQueries({ queryKey: runKeys.state(run?.workflow_id ?? "", sessionId) });
  }

  const pause = useMutation({
    mutationFn: () => sessionsApi.pauseRun(sessionId ?? ""),
    onSuccess: (result) => {
      if (result.ok) {
        setActionNotice(null);
        invalidateRun();
        queryClient.invalidateQueries({ queryKey: runKeys.live });
      } else {
        setActionNotice(result.message ?? "");
      }
    },
    onError: (error) => setActionNotice(studioErrorMessage(error)),
  });
  const resume = useMutation({
    mutationFn: () => sessionsApi.resumeRun(sessionId ?? ""),
    onSuccess: (result) => {
      if (result.ok) {
        setActionNotice(null);
        invalidateRun();
        queryClient.invalidateQueries({ queryKey: runKeys.live });
      } else {
        setActionNotice(result.message ?? "");
      }
    },
    onError: (error) => setActionNotice(studioErrorMessage(error)),
  });
  const stop = useMutation({
    mutationFn: () => sessionsApi.stopRun(sessionId ?? ""),
    onSuccess: (result) => {
      if (result.ok) {
        setActionNotice(null);
        invalidateRun();
        queryClient.invalidateQueries({ queryKey: runKeys.live });
      } else {
        setActionNotice(result.message ?? "");
      }
    },
    onError: (error) => setActionNotice(studioErrorMessage(error)),
    onSettled: () => setConfirmingStop(false),
  });
  const automode = useMutation({
    mutationFn: (on: boolean) => sessionsApi.setAutomode(sessionId ?? "", on),
    onSuccess: invalidateRun,
  });

  // Live means only two of the three modes are reachable through this control (`setAutomode`
  // flips between "wait" and "auto"): the orchestrator can decide a gate only when the run
  // started that way, and there is no control here to switch a live run into it.
  const liveGateMode = runState.data?.gate_mode;
  const displayedGateMode: GateMode =
    run !== null && liveGateMode !== undefined ? liveGateMode : gateMode;

  function changeGateMode(next: GateMode) {
    if (run !== null) {
      automode.mutate(next === "auto");
    } else {
      onGateModeChange(next);
    }
  }

  const session = detail.data?.session;

  return (
    <div className="mx-auto w-full max-w-[1400px] px-4 pb-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-label-3">
        {sessionId !== null && session !== undefined ? (
          <VitalsReadout
            costUsd={session.cost_usd}
            unpriced={session.unpriced}
            context={session.context}
            tokensIn={session.usage.input_tokens}
            tokensOut={session.usage.output_tokens}
            turnStartedAt={turnStartedAt}
            now={turnNow}
          />
        ) : null}

        <div className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1">
          <fieldset className="flex items-center rounded-full border border-line bg-well p-0.5">
            <legend className="sr-only">Who decides a gate this run reaches?</legend>
            {GATE_MODES.map((mode, index) => {
              const words = GATE_MODE_WORDS[mode];
              const checked = mode === displayedGateMode;
              const disabled = mode === "orchestrate" && run !== null;
              const description = disabled
                ? "A live run cannot hand its gates to the orchestrator."
                : words.description;
              return (
                <label
                  key={mode}
                  title={description}
                  className={cn(
                    "flex h-6 items-center whitespace-nowrap rounded-full px-2.5 text-micro transition-colors",
                    "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent has-[:focus-visible]:ring-offset-1 has-[:focus-visible]:ring-offset-well",
                    checked
                      ? "bg-label-1 font-semibold text-surface shadow-sm"
                      : "text-label-2 hover:text-label-1",
                    disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
                  )}
                >
                  <input
                    ref={index === 0 ? firstGateRadioRef : undefined}
                    type="radio"
                    name="chat-gate-mode"
                    value={mode}
                    checked={checked}
                    disabled={disabled}
                    aria-label={words.label}
                    onChange={() => changeGateMode(mode)}
                    className="sr-only"
                  />
                  <span>{words.short}</span>
                </label>
              );
            })}
          </fieldset>

          <Link
            href="/harness"
            title="What the agent may do here. Open Harness."
            className="whitespace-nowrap hover:text-label-1 hover:underline"
          >
            {openPluginCount} {openPluginCount === 1 ? "plugin" : "plugins"}
          </Link>

          {run !== null ? (
            <>
              {run.state === "paused" ? (
                <button
                  type="button"
                  onClick={() => resume.mutate()}
                  disabled={resume.isPending}
                  className="whitespace-nowrap text-label-2 hover:text-label-1 hover:underline disabled:opacity-50"
                >
                  Resume
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => pause.mutate()}
                  disabled={pause.isPending}
                  className="whitespace-nowrap text-label-2 hover:text-label-1 hover:underline disabled:opacity-50"
                >
                  Pause
                </button>
              )}
              <button
                type="button"
                onClick={() => setConfirmingStop(true)}
                disabled={stop.isPending}
                className="whitespace-nowrap text-danger-ink hover:underline disabled:opacity-50"
              >
                Stop
              </button>
            </>
          ) : null}

          {sessionId !== null ? (
            <ClearControl
              masterId={sessionId}
              onCleared={onCleared}
              title="Start this session over?"
            />
          ) : null}
        </div>
      </div>

      {actionNotice !== null ? (
        <p role="alert" className="mt-1 text-caption text-warn-ink">
          {actionNotice}
        </p>
      ) : null}

      <ConfirmDialog
        open={confirmingStop}
        title="Stop for good?"
        message="Nothing will be resumable."
        confirmLabel="Stop"
        destructive
        confirming={stop.isPending}
        onConfirm={() => stop.mutate()}
        onCancel={() => setConfirmingStop(false)}
      />
    </div>
  );
}
