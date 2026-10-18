"use client";

import { formatRelative } from "client-core/lib/format-relative";
import type { ReactElement, ReactNode } from "react";

import type { RunView } from "@/api/sessions-types";
import type { Outcome, RunEnvelope } from "@/api/studio-types";
import { ProvenanceChip } from "@/components/marks/provenance-chip";
import { RUN_STATE_PILL, type RunStatePill } from "@/components/marks/run-state";
import { StatePill, type PillTone } from "@/components/marks/state-pill";
import { formatElapsedMs, useNowTick } from "@/components/workflows/elapsed";
import { runnerActor, runnerLabel } from "@/components/workflows/runner-label";
import { spendText } from "@/lib/cost-text";

// The run's own outcome at a glance, a 5-card row above the gate trail: what it is doing or
// did, when it started, how long it has run, who set it going, and what it has cost. A live
// run's own envelope carries `outcome: "live"`, refined by the live pool's own finer state
// (running, paused, waiting on you). An archived run reads its outcome straight off the record.

export interface OutcomeStripProps {
  envelope: RunEnvelope;
  liveState: RunView["state"] | null;
}

type ArchivedOutcome = Exclude<RunEnvelope["outcome"], "live">;

const ARCHIVED_PILL: Record<ArchivedOutcome, RunStatePill> = {
  completed: RUN_STATE_PILL.completed,
  failed: RUN_STATE_PILL.failed,
  stopped: RUN_STATE_PILL.stopped,
};

const LIVE_STATE_PILL: Record<NonNullable<RunView["state"]>, RunStatePill> = {
  running: RUN_STATE_PILL.running,
  paused: RUN_STATE_PILL.paused,
  waiting: RUN_STATE_PILL.waiting,
};

function outcomePill(envelope: RunEnvelope, liveState: RunView["state"] | null): RunStatePill {
  if (envelope.outcome !== "live") return ARCHIVED_PILL[envelope.outcome];
  return liveState !== null ? LIVE_STATE_PILL[liveState] : RUN_STATE_PILL.running;
}

function runDuration(envelope: RunEnvelope, now: number): string {
  const startedMs = new Date(envelope.started_at).getTime();
  const endedMs = envelope.ended_at !== "" ? new Date(envelope.ended_at).getTime() : now;
  return formatElapsedMs(endedMs - startedMs);
}

// "unknown" when no ledger exists for the run yet, rather than a cost of zero: what we do not know
// is not free.
function runCostText(envelope: RunEnvelope): string {
  const spend = envelope.spend;
  return spend !== null && spend !== undefined
    ? spendText(spend.cost_usd, spend.unpriced)
    : "unknown";
}

function StatCell({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-surface px-4 py-3.5 shadow-sm">
      <p className="text-caption text-label-3">{label}</p>
      <div className="mt-1 text-body font-medium text-label-1">{children}</div>
    </div>
  );
}

export function OutcomeStrip({ envelope, liveState }: OutcomeStripProps): ReactElement {
  const now = useNowTick(1000, envelope.outcome === "live");
  const pill = outcomePill(envelope, liveState);

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      <StatCell label="Outcome">
        <StatePill tone={pill.tone} size="sm">
          {pill.word}
        </StatePill>
      </StatCell>
      <StatCell label="Started">
        <span title={new Date(envelope.started_at).toLocaleString()}>
          {formatRelative(envelope.started_at, now)}
        </span>
      </StatCell>
      <StatCell label="Duration">
        <span className="font-mono">{runDuration(envelope, now)}</span>
      </StatCell>
      <StatCell label="Started by">
        <ProvenanceChip actor={runnerActor(envelope.runner)} variant="inline">
          {runnerLabel(envelope.runner)}
        </ProvenanceChip>
      </StatCell>
      <StatCell label="Cost">
        <span className="font-mono">{runCostText(envelope)}</span>
      </StatCell>
    </div>
  );
}

// -- the checked result -------------------------------------------------------------------------

// What a run did, in one word and one sentence, read only off the facts the host checked. A
// produced value is never evidence here: work that was skipped is not work that was done, and a
// run whose evidence nobody could read says exactly that instead of claiming anything. A reading
// states the counts it was given and nothing beyond them: a run can fail with its work already
// succeeded (a gate that spent its last reach), so the status word is the verdict and the counts
// stay counts.

export interface RecordedOutput {
  label: string;
  value: string;
}

type ResultStatus = Outcome["status"];

// Five statuses, five tones. `noop` and `unknown` are the pair a reader is most likely to conflate
// and the two that must never share a mark: nothing needed doing is a settled answer, and nothing
// can be established is the absence of one.
export const RESULT_STATUS: Record<ResultStatus, { tone: PillTone; word: string }> = {
  succeeded: { tone: "completed", word: "Succeeded" },
  noop: { tone: "queued", word: "No work needed" },
  partial: { tone: "waiting", word: "Partly done" },
  failed: { tone: "failed", word: "Work failed" },
  unknown: { tone: "warning", word: "Unchecked" },
};

const UNCHECKED = "Result evidence is unavailable, so what this run did cannot be checked.";

export function resultReading(result: Outcome | null | undefined): string {
  if (!result) return UNCHECKED;
  const { attempted, succeeded, skipped, failed } = result;
  switch (result.status) {
    case "succeeded":
      return `${succeeded} of ${attempted} succeeded, checked against the run's own facts.`;
    case "noop":
      return "There was nothing to do, so no work was needed.";
    case "partial":
      return `${succeeded} of ${attempted} succeeded, ${skipped} skipped, ${failed} failed.`;
    case "failed":
      return `${failed} of ${attempted} failed, ${skipped} skipped, ${succeeded} succeeded.`;
    default:
      return UNCHECKED;
  }
}

export interface ResultSummaryProps {
  result: Outcome | null | undefined;
  // Counters the workflow's author asked to show, read straight off what the run recorded. They sit
  // beside the status and never change it: a number a run wrote about itself settles nothing.
  recorded?: RecordedOutput[];
  compact?: boolean;
}

export function ResultSummary({
  result,
  recorded = [],
  compact = false,
}: ResultSummaryProps): ReactElement {
  const status = RESULT_STATUS[result?.status ?? "unknown"];
  const summary = result?.summary ?? "";
  return (
    <div
      className={
        compact
          ? "flex flex-col gap-1.5"
          : "flex flex-col gap-2 rounded-lg border border-line bg-surface px-4 py-3 shadow-sm"
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatePill tone={status.tone} size="sm">
          {status.word}
        </StatePill>
        <span className="text-body text-label-1">{resultReading(result)}</span>
      </div>
      {summary !== "" ? <p className="text-caption text-label-3">{summary}</p> : null}
      {recorded.length > 0 ? (
        <div className="flex flex-col gap-1 border-line border-t pt-2">
          <p className="text-caption text-label-3">Recorded output</p>
          <dl className="flex flex-wrap gap-x-6 gap-y-1">
            {recorded.map((fact) => (
              <div key={fact.label} className="flex items-baseline gap-1.5">
                <dt className="text-caption text-label-2">{fact.label}</dt>
                <dd className="font-mono text-caption text-label-1">{fact.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
    </div>
  );
}
