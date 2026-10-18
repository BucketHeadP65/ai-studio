"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatRelative } from "client-core/lib/format-relative";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { Card } from "client-core/ui/card";
import { Input } from "client-core/ui/input";
import { Select } from "client-core/ui/select";
import { useEffect, useState } from "react";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";
import type { Job, RefreshRun, Schedule } from "@/api/types";

interface Props {
  name: string;
  registered: boolean;
  lastRun: RefreshRun | null;
  schedule: Schedule | null;
}

// A job in one of these states is over: the worker will not touch it again.
function isOver(job: Job | undefined): boolean {
  return job?.state === "done" || job?.state === "abandoned";
}

// The knowledge base refreshes itself from here: one press queues the same sync-then-index the CLI
// runs (embeddings always on), the background worker runs it, and every finished run lands on the
// knowledge base's own refresh history. The registry row may declare a cadence the worker's ticker
// queues on the same terms. Scheduled runs land on the same refresh history wearing their trigger.
export function RefreshCard({ name, registered, lastRun, schedule }: Props) {
  const queryClient = useQueryClient();
  const refreshRunsKey = knowledgeBaseKeys.refreshRuns(name);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: knowledgeBaseKeys.detail(name) });
    queryClient.invalidateQueries({ queryKey: refreshRunsKey });
  };

  const refreshRuns = useQuery({
    queryKey: refreshRunsKey,
    queryFn: () => knowledgeBaseApi.refreshRuns(name),
    enabled: registered,
  });

  const refresh = useMutation({
    mutationFn: () => knowledgeBaseApi.startRefresh(name),
    onSettled: invalidate,
  });

  // The card follows the job it queued until the worker is done with it, so the queued line lasts
  // as long as the run does and not a moment longer.
  const jobId = refresh.data?.jobId ?? null;
  const job = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => knowledgeBaseApi.readJob(jobId ?? 0),
    enabled: jobId !== null,
  });

  // A finished run belongs in the refresh history and on the knowledge base's own vitals, so both are
  // refetched the moment the job reaches its last state.
  const finishedJobId = isOver(job.data) ? jobId : null;
  useEffect(() => {
    if (finishedJobId !== null) {
      queryClient.invalidateQueries({ queryKey: knowledgeBaseKeys.detail(name) });
    }
  }, [finishedJobId, name, queryClient]);

  return (
    <Card className="px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="font-medium text-sm text-label-1">Refresh</h2>
          <p className="mt-0.5 text-caption text-label-3">
            Sync the sources and index again, embeddings on, the same run the CLI does.
          </p>
        </div>
        <Button
          className="h-7 shrink-0 px-2.5 text-caption"
          onClick={() => refresh.mutate()}
          disabled={!registered || refresh.isPending}
        >
          {refresh.isPending ? "Refreshing..." : "Refresh now"}
        </Button>
      </div>
      {refresh.data && !isOver(job.data) ? (
        <p className="mt-2 text-caption text-label-3">
          {refresh.data.alreadyRunning
            ? `A run for this knowledge base is already underway (job ${refresh.data.jobId}).`
            : `Queued as job ${refresh.data.jobId}. The worker runs it, and the refresh history below shows the outcome.`}
        </p>
      ) : null}
      {refresh.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {knowledgeBaseErrorMessage(refresh.error)}
        </p>
      ) : null}

      {lastRun ? <RunRow record={lastRun} label="last run" /> : null}

      {refreshRuns.data && refreshRuns.data.refresh_runs.length > 1 ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-caption text-label-3">
            Earlier runs, {refreshRuns.data.refresh_runs.length - 1}
          </summary>
          <ul className="mt-1 divide-y divide-line">
            {refreshRuns.data.refresh_runs.slice(1).map((record) => (
              <li key={record.started_at}>
                <RunRow record={record} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <ScheduleRow name={name} registered={registered} schedule={schedule} onChanged={invalidate} />
    </Card>
  );
}

function RunRow({ record, label }: { record: RefreshRun; label?: string }) {
  return (
    <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      {label ? <span className="text-caption text-label-3">{label}</span> : null}
      <Badge tone={record.outcome === "ok" ? "ok" : "danger"} className="normal-case">
        {record.outcome}
      </Badge>
      {record.trigger === "schedule" ? (
        <Badge tone="neutral" className="normal-case">
          scheduled
        </Badge>
      ) : null}
      <span className="text-caption text-label-3 tabular-nums">
        {formatRelative(record.finished_at)}
      </span>
      <span className="min-w-0 flex-1 text-caption text-label-2">{record.detail}</span>
    </div>
  );
}

function describeSchedule(schedule: Schedule | null): string {
  if (schedule?.every_seconds) {
    return `every ${Math.round(schedule.every_seconds / 60)} min`;
  }
  if (schedule?.daily_at) {
    return `daily at ${schedule.daily_at}`;
  }
  return "none";
}

function ScheduleRow({
  name,
  registered,
  schedule,
  onChanged,
}: {
  name: string;
  registered: boolean;
  schedule: Schedule | null;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState<"every" | "daily">("every");
  const [minutes, setMinutes] = useState("60");
  const [clock, setClock] = useState("06:00");

  const save = useMutation({
    mutationFn: () =>
      knowledgeBaseApi.setSchedule(
        name,
        mode === "every"
          ? { every_seconds: Math.max(5, Number(minutes) || 60) * 60 }
          : { daily_at: clock },
      ),
    onSuccess: () => {
      setEditing(false);
      onChanged();
    },
  });
  const clear = useMutation({
    mutationFn: () => knowledgeBaseApi.clearSchedule(name),
    onSuccess: onChanged,
  });

  return (
    <div className="mt-3 border-line border-t pt-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-caption text-label-3">
          schedule{" "}
          <span className="font-medium text-body text-label-2">{describeSchedule(schedule)}</span>
        </span>
        {schedule ? (
          <Badge tone="ok" className="normal-case">
            runs while the worker is up
          </Badge>
        ) : null}
        <span className="flex-1" />
        {editing ? (
          <span className="flex items-center gap-2">
            <Select
              value={mode}
              onChange={(event) => setMode(event.target.value === "daily" ? "daily" : "every")}
              aria-label="Schedule type"
              className="h-7 py-0"
            >
              <option value="every">every N minutes</option>
              <option value="daily">daily at</option>
            </Select>
            {mode === "every" ? (
              <Input
                value={minutes}
                onChange={(event) => setMinutes(event.target.value)}
                aria-label="Minutes between runs"
                inputMode="numeric"
                className="h-7 w-16 py-0 text-caption"
              />
            ) : (
              <Input
                value={clock}
                onChange={(event) => setClock(event.target.value)}
                aria-label="Time of day"
                placeholder="06:00"
                className="h-7 w-20 py-0 text-caption"
              />
            )}
            <Button
              className="h-7 px-2.5 text-caption"
              onClick={() => save.mutate()}
              disabled={save.isPending}
            >
              Save
            </Button>
            <Button
              variant="secondary"
              className="h-7 px-2.5 text-caption"
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
          </span>
        ) : (
          <span className="flex items-center gap-2">
            <Button
              variant="secondary"
              className="h-7 px-2.5 text-caption"
              onClick={() => setEditing(true)}
              disabled={!registered}
            >
              {schedule ? "Change" : "Set schedule"}
            </Button>
            {schedule ? (
              <Button
                variant="secondary"
                className="h-7 px-2.5 text-caption"
                onClick={() => clear.mutate()}
                disabled={clear.isPending}
              >
                Clear
              </Button>
            ) : null}
          </span>
        )}
      </div>
      {save.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {knowledgeBaseErrorMessage(save.error)}
        </p>
      ) : null}
    </div>
  );
}
