"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { EmptyState } from "client-core/ui/empty-state";
import { CalendarClock } from "lucide-react";
import { type ReactElement, useState } from "react";

import { scheduleKeys } from "@/api/query-keys";
import type { Schedule } from "@/api/schedules";
import { schedulesApi } from "@/api/schedules";
import { studioApi, studioErrorMessage } from "@/api/studio";
import { ScheduleCard, formatNextRun } from "@/components/workflows/schedule-card";
import { ScheduleForm, type ScheduleFormPayload } from "@/components/workflows/schedule-form";

// The schedules station at `/workflows/schedules`: management home for every standing,
// cadence-driven trigger, one vertical stack with every schedule on, soonest fire first, then
// every paused one. A card's own name is its workflow's title, read off the library and joined by
// workflow id; a workflow the library no longer carries falls back to reading its own id.

function sortByNextRun(schedules: Schedule[]): Schedule[] {
  return [...schedules].sort((a, b) => {
    if (a.next_run_at === null && b.next_run_at === null) return 0;
    if (a.next_run_at === null) return 1;
    if (b.next_run_at === null) return -1;
    return a.next_run_at.localeCompare(b.next_run_at);
  });
}

// The header's one summary sentence: how many schedules stand, how many are on versus paused
// (a zero part dropped rather than said as "0 on" or "0 paused"), and the soonest an enabled one
// will fire next.
function summarizeSchedules(schedules: Schedule[]): string {
  const total = schedules.length;
  if (total === 0) return "No schedules yet.";
  const on = schedules.filter((schedule) => schedule.enabled).length;
  const paused = total - on;
  const parts: string[] = [];
  if (on > 0) parts.push(`${on} on`);
  if (paused > 0) parts.push(`${paused} paused`);
  const nextFires = schedules
    .filter((schedule): schedule is Schedule & { next_run_at: string } => {
      return schedule.enabled && schedule.next_run_at !== null;
    })
    .map((schedule) => schedule.next_run_at)
    .sort();
  const nextSentence = nextFires.length > 0 ? ` Next fire ${formatNextRun(nextFires[0])}.` : "";
  return `${total} schedule${total === 1 ? "" : "s"}, ${parts.join(", ")}.${nextSentence}`;
}

export function SchedulesPage(): ReactElement {
  const queryClient = useQueryClient();
  const [formOpen, setFormOpen] = useState(false);
  const [condemned, setCondemned] = useState<string | null>(null);

  const schedulesQuery = useQuery({
    queryKey: scheduleKeys.all,
    queryFn: () => schedulesApi.list(),
  });
  const library = useQuery({ queryKey: ["library"], queryFn: () => studioApi.library() });

  const invalidateSchedules = () => queryClient.invalidateQueries({ queryKey: scheduleKeys.all });
  const toggleSchedule = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      schedulesApi.update(id, { enabled }),
    onSettled: invalidateSchedules,
  });
  const runScheduleNow = useMutation({
    mutationFn: (id: string) => schedulesApi.runNow(id),
    onSettled: invalidateSchedules,
  });
  const deleteSchedule = useMutation({
    mutationFn: (id: string) => schedulesApi.remove(id),
    onSuccess: () => setCondemned(null),
    onSettled: invalidateSchedules,
  });
  const createSchedule = useMutation({
    mutationFn: (payload: ScheduleFormPayload): Promise<Schedule> =>
      schedulesApi.create({
        workflow_id: payload.workflowId,
        arguments: payload.arguments,
        cadence: payload.cadence,
        gate_policy: payload.gatePolicy,
        gate_mode: payload.gateMode,
        capacity_key: payload.capacityKey,
        capacity_limit: payload.capacityLimit,
        blackout: payload.blackout,
        catch_up: payload.catchUp,
      }),
    onSuccess: () => {
      setFormOpen(false);
      invalidateSchedules();
    },
  });

  const schedules = schedulesQuery.data ?? [];
  const ordered = [
    ...sortByNextRun(schedules.filter((schedule) => schedule.enabled)),
    ...sortByNextRun(schedules.filter((schedule) => !schedule.enabled)),
  ];
  const titleByWorkflowId = new Map((library.data ?? []).map((card) => [card.id, card.title]));

  function nameFor(workflowId: string): string {
    return titleByWorkflowId.get(workflowId) ?? workflowId;
  }

  return (
    <div className="-m-6 flex flex-col">
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Schedules</h1>
          <p className="truncate text-caption text-label-3">
            {schedulesQuery.data
              ? summarizeSchedules(schedules)
              : "every standing, cadence-driven trigger"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2.5">
          <Button onClick={() => setFormOpen(true)}>New schedule</Button>
        </div>
      </div>

      <div className="flex flex-col gap-4 px-7 pt-5 pb-7">
        {schedulesQuery.isError ? (
          <p
            role="alert"
            className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
          >
            {studioErrorMessage(schedulesQuery.error)}
          </p>
        ) : null}

        {schedulesQuery.isPending ? (
          <p className="text-body text-label-3">Loading schedules.</p>
        ) : null}

        {schedulesQuery.data && schedules.length === 0 ? (
          <EmptyState
            icon={<CalendarClock size={20} strokeWidth={1.5} />}
            title="Nothing scheduled yet."
            hint="Add one with New schedule, or ask the agent in chat to schedule one."
          >
            <Button onClick={() => setFormOpen(true)}>New schedule</Button>
          </EmptyState>
        ) : null}

        {ordered.length > 0 ? (
          <ul className="flex flex-col gap-3">
            {ordered.map((schedule) => (
              <ScheduleCard
                key={schedule.id}
                schedule={schedule}
                name={nameFor(schedule.workflow_id)}
                togglingEnabled={
                  toggleSchedule.isPending && toggleSchedule.variables?.id === schedule.id
                }
                runningNow={runScheduleNow.isPending && runScheduleNow.variables === schedule.id}
                onToggleEnabled={() =>
                  toggleSchedule.mutate({ id: schedule.id, enabled: !schedule.enabled })
                }
                onRunNow={() => runScheduleNow.mutate(schedule.id)}
                onDelete={() => setCondemned(schedule.id)}
              />
            ))}
          </ul>
        ) : null}
      </div>

      <ScheduleForm
        open={formOpen}
        submitting={createSchedule.isPending}
        error={createSchedule.isError ? studioErrorMessage(createSchedule.error) : null}
        onSubmit={(payload) => createSchedule.mutate(payload)}
        onCancel={() => setFormOpen(false)}
      />

      <ConfirmDialog
        open={condemned !== null}
        title="Delete this schedule?"
        message="Its standing trigger is removed for good. A run it already started keeps going."
        confirmLabel="Delete it"
        destructive
        confirming={deleteSchedule.isPending}
        onConfirm={() => condemned !== null && deleteSchedule.mutate(condemned)}
        onCancel={() => setCondemned(null)}
      />
    </div>
  );
}
