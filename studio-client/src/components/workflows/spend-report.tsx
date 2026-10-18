"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactElement } from "react";

import { harnessApi } from "@/api/harness";
import type { SpendAnswer, SpendLine } from "@/api/harness-types";
import { spendKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { costText } from "@/lib/cost-text";

// A run's own spend, read straight from the telemetry: a stat cell styled after OutcomeStrip's,
// carrying the total sentence and one row per key the telemetry grouped calls under. An
// unavailable report shows why the telemetry could not answer and where a narrower answer still
// lives, rather than guessing at a figure.

const LABEL = "Spend (telemetry)";
const SINCE_HOURS = 720;

export interface SpendReportProps {
  runId: string;
}

function Row({ line }: { line: SpendLine }): ReactElement {
  return (
    <li className="flex items-center justify-between gap-3 py-1 text-caption">
      <span className="truncate text-label-1">{line.key}</span>
      <span className="shrink-0 font-mono text-label-2">{costText(line.cost_usd)}</span>
      <span className="shrink-0 text-label-3">{line.calls} calls</span>
    </li>
  );
}

function SpendBody({ answer }: { answer: SpendAnswer }): ReactElement {
  if (!answer.available) {
    return (
      <p>
        {answer.reason}. {answer.fallback}
      </p>
    );
  }
  if (answer.lines.length === 0) {
    return <p>No priced model calls for this run in the last 30 days.</p>;
  }
  return (
    <div>
      <p>
        {costText(answer.total.cost_usd)} across {answer.total.calls} calls, last 30 days
      </p>
      <ul className="mt-2 divide-y divide-line">
        {answer.lines.map((line) => (
          <Row key={line.key} line={line} />
        ))}
      </ul>
    </div>
  );
}

export function SpendReport({ runId }: SpendReportProps): ReactElement {
  const spend = useQuery({
    queryKey: spendKeys.run(runId),
    queryFn: () => harnessApi.spend("step", runId, SINCE_HOURS),
    refetchInterval: false,
    staleTime: 5 * 60_000,
  });

  return (
    <div className="rounded-lg border border-line bg-surface px-4 py-3.5 shadow-sm">
      <p className="text-caption text-label-3">{LABEL}</p>
      <div className="mt-1 text-body font-medium text-label-1">
        {spend.isError ? (
          <p className="text-warn-ink">{studioErrorMessage(spend.error)}</p>
        ) : spend.data !== undefined ? (
          <SpendBody answer={spend.data} />
        ) : (
          "pricing"
        )}
      </div>
    </div>
  );
}
