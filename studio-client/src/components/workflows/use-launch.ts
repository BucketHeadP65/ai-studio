"use client";

import { useMutation } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import { type ReconciledRun, launchWorkflowRun, reconcileRun } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { RunIntent } from "@/api/studio-types";

// One deliberate start, and what to do when its answer never came back.
//
// Three rules live here rather than in each screen that offers a Run button, so the two screens
// that do cannot come to mean different things by the same click.
//
// A start is sent once. The mutation never retries, and a second press while one is in flight is
// dropped: a request that got no answer may already have made a run, and sending it again would
// make a second one nothing asked for. The drop is read off a latch rather than off the rendered
// pending flag, because two presses can land inside one render and the rendered flag is still
// false for the second of them.
//
// A start whose answer never came back is unknown, not failed and not succeeded. What settles it
// is reading the runs themselves, which the person asks for. Whatever they typed stays where they
// typed it, so starting again afterwards is a decision they make, never one this makes for them.
//
// What the last start was set up with is kept, because that is what a rerun reopens.

/** What a screen needs of one launch: how to start it, where it stands, and how to settle it. */
export interface DeliberateLaunch {
  /** Send this setup, once. Ignored while a start is already in flight. */
  start: (intent: RunIntent) => void;
  starting: boolean;
  /** A refusal the backend actually answered with, or `null`. */
  error: string | null;
  /** Why the last start's outcome is unknown, or `null` when there is nothing in doubt. */
  unknownReason: string | null;
  /** Read the runs and see whether the unknown start made one. Sends no start. */
  reconcile: () => void;
  reconciling: boolean;
  /** True once the reading has answered that no run was made at all. */
  settledAsNothing: boolean;
  /** The setup the last start was made with, which a rerun reopens. */
  lastIntent: RunIntent | null;
  /** Forget the last start's outcome, for a setup being opened fresh. */
  reset: () => void;
}

export interface DeliberateLaunchOptions {
  /** What the runner session a launch opens is called. The run's own name rides the intent. */
  sessionTitle: string;
  /** The run started, in this session. */
  onStarted: (sessionId: string) => void;
  /** An unknown start turned out to have made this run after all. */
  onReconciled?: (found: ReconciledRun) => void;
}

export function useDeliberateLaunch({
  sessionTitle,
  onStarted,
  onReconciled,
}: DeliberateLaunchOptions): DeliberateLaunch {
  const [unknown, setUnknown] = useState<{ sessionId: string; reason: string } | null>(null);
  const [lastIntent, setLastIntent] = useState<RunIntent | null>(null);
  const [settledAsNothing, setSettledAsNothing] = useState(false);
  // True from the moment a start is handed over until it has answered, however it answers. The
  // rendered pending flag says the same thing one render later, which is too late for a second
  // press in the same one.
  const inFlight = useRef(false);

  const start = useMutation({
    // Never retried. The default is already no retry for a mutation. Said out loud because a start
    // is the one request where a retry would be a second execution rather than a second attempt.
    retry: 0,
    mutationFn: (intent: RunIntent) => launchWorkflowRun(intent, sessionTitle),
    onMutate: (intent: RunIntent) => {
      setLastIntent(intent);
      setUnknown(null);
      setSettledAsNothing(false);
    },
    onSuccess: (outcome) => {
      if (outcome.state === "started") {
        onStarted(outcome.sessionId);
        return;
      }
      setUnknown({ sessionId: outcome.sessionId, reason: outcome.reason });
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const settle = useMutation({
    retry: 0,
    mutationFn: (sessionId: string) => reconcileRun(sessionId),
    onSuccess: (found) => {
      setSettledAsNothing(found === null);
      if (found === null) return;
      setUnknown(null);
      onReconciled?.(found);
    },
  });

  const reset = useCallback(() => {
    inFlight.current = false;
    setUnknown(null);
    setSettledAsNothing(false);
    start.reset();
    settle.reset();
  }, [start, settle]);

  return {
    start: (intent) => {
      if (inFlight.current) return;
      inFlight.current = true;
      start.mutate(intent);
    },
    starting: start.isPending,
    error: start.isError ? studioErrorMessage(start.error) : null,
    unknownReason: unknown?.reason ?? null,
    reconcile: () => {
      if (unknown === null || settle.isPending) return;
      settle.mutate(unknown.sessionId);
    },
    reconciling: settle.isPending,
    settledAsNothing,
    lastIntent,
    reset,
  };
}
