"use client";

import type { ReactElement } from "react";

import type { PendingGate, StepEvent } from "@/api/studio-types";
import { NodeGlyph } from "@/components/marks/node-glyph";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";

import { GatePanel } from "./gate-panel";

// The transcript's own trailing item while this session's live run waits at a gate: how long it
// has been waiting, and the same panel a person answers it from everywhere else in the app.
// `events` is the live run's own transition log. The elapsed time reads the gate's own unfinished
// event (the one this gate is waiting at) rather than the run's start, so a run that ran for a
// while before reaching this gate doesn't inflate the wait.

export interface GateTurnProps {
  pending: PendingGate;
  startedAt: string;
  events: StepEvent[];
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
}

export function GateTurn({
  pending,
  startedAt,
  events,
  onDecide,
  deciding,
}: GateTurnProps): ReactElement {
  const now = useNowTick();
  const event = events.find(
    (candidate) => candidate.node === pending.gate_id && !candidate.finished,
  );
  const elapsed =
    event === undefined || startedAt === ""
      ? ""
      : formatElapsedMs(now - (new Date(startedAt).getTime() + event.at * 1000));

  return (
    <div className="max-w-[92%] self-start overflow-hidden rounded-tl-sm rounded-tr-lg rounded-br-lg rounded-bl-lg border border-warn shadow-sm">
      <div className="flex items-center gap-2.5 border-warn/25 border-b bg-warn-soft px-4 py-3">
        <NodeGlyph kind="person-gate" fill="waiting" size={20} />
        <p className="flex-1 text-caption text-label-1">
          <span className="font-mono">{pending.gate_id}</span> is waiting for you.
        </p>
        {elapsed !== "" ? (
          <span className="font-mono text-caption text-warn-ink">{elapsed}</span>
        ) : null}
      </div>
      <div className="bg-surface px-4 py-3.5">
        <GatePanel pending={pending} onDecide={onDecide} deciding={deciding} chrome="bare" />
      </div>
    </div>
  );
}
