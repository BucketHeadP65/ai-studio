"use client";

import { cn } from "@/shared/lib/cn";
import { formatRelative } from "@/shared/lib/format-relative";
import { Button } from "@/shared/ui/button";
import { CheckCircle2, MinusCircle, Trash2, XCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { MouseEvent, ReactElement, ReactNode } from "react";

import type { Schedule } from "@/api/schedules";
import { GATE_MODE_WORDS } from "@/components/marks/gate-mode";
import { RUN_STATE_PILL } from "@/components/marks/run-state";
import { type PillTone, StatePill } from "@/components/marks/state-pill";
import { OpenRunLink } from "@/components/workflows/open-run-link";
import { spendText } from "@/lib/cost-text";

// One card on the schedules station: a workflow's standing, cadence-driven trigger, with the
// acts an owner has over it inline (enable/disable, run it now, delete it) and the state a
// glance needs (what it is doing now, what its cadence and property tags are, how its last fire
// went). A paused schedule reads as a single sentence instead, since nothing on it can move.
// The whole card opens the workflow; a click on one of the card's own controls keeps its own
// meaning instead. Editing a schedule's cadence or arguments is the create form's job, not this
// card's -- a card only ever shows what a schedule already carries.

const INTERVAL = /^every\s+(\d+)\s*(s|m|h|d)$/i;
const UNIT_WORD: Record<string, string> = { s: "second", m: "minute", h: "hour", d: "day" };

// The raw cadence text ("every 30m", or a 5-field cron expression) in words. A cron expression
// has no cheap, always-correct English rendering without a library this app does not carry, so
// it reads as its own expression, named as one.
export function cadenceWords(cadence: string): string {
  const match = INTERVAL.exec(cadence.trim());
  if (match === null) {
    return `on cron schedule ${cadence}`;
  }
  const [, count, unit] = match;
  const n = Number(count);
  const word = UNIT_WORD[unit.toLowerCase()] ?? unit;
  return `every ${n} ${word}${n === 1 ? "" : "s"}`;
}

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

// A future instant in words ("in 5 minutes", "in 2 hours") -- `formatRelative` (shared/lib)
// only ever reads a past instant correctly, so this is its future-facing sibling. `now` is a
// parameter, not `Date.now()` read inline, so a test can pin it.
export function formatNextRun(iso: string | null, now: number = Date.now()): string {
  if (iso === null) return "not scheduled";
  const diffMinutes = Math.round((new Date(iso).getTime() - now) / 60_000);
  if (diffMinutes <= 0) return "due now";
  if (diffMinutes < 60) return RELATIVE.format(diffMinutes, "minute");
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) return RELATIVE.format(diffHours, "hour");
  return RELATIVE.format(Math.round(diffHours / 24), "day");
}

function capitalize(words: string): string {
  return words.length === 0 ? words : `${words[0]?.toUpperCase()}${words.slice(1)}`;
}

// A tick's own outcome, read loosely since the backend still writes a free sentence into
// `last_outcome` ("launched", "held, capacity ... is full", "failed, ...", "refused: ..."). A
// failed or refused tick reads as a failure; anything else that has fired reads as caught up.
function outcomeFailed(lastOutcome: string): boolean {
  return (
    lastOutcome === "stopped" ||
    lastOutcome.startsWith("failed") ||
    lastOutcome.startsWith("refused")
  );
}

// The property tags a cadence row draws, in order: a capacity pool (with its limit appended
// only once the pair is set), a blackout window, whether a held fire catches up or is skipped,
// and how this schedule's own gates are decided. The last two always render, since both fields
// always carry a value; the first two render only once their own field is set.
function propertyTags(schedule: Schedule): string[] {
  const tags: string[] = [];
  if (schedule.capacity_key !== null) {
    const limit = schedule.capacity_limit !== null ? ` limit ${schedule.capacity_limit}` : "";
    tags.push(`pool ${schedule.capacity_key}${limit}`);
  }
  if (schedule.blackout !== null) {
    tags.push(`quiet ${schedule.blackout}`);
  }
  tags.push(schedule.catch_up ? "catches up" : "skips missed");
  tags.push(GATE_MODE_WORDS[schedule.gate_mode].tag);
  return tags;
}

function MonoTag({ children }: { children: ReactNode }): ReactElement {
  return (
    <span className="shrink-0 rounded-full border border-line px-2.5 py-1 font-mono text-label-2 text-micro">
      {children}
    </span>
  );
}

function ScheduleSwitch({
  checked,
  busy,
  onFlip,
}: {
  checked: boolean;
  busy: boolean;
  onFlip: () => void;
}): ReactElement {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={checked ? "Schedule on" : "Schedule off"}
      disabled={busy}
      onClick={onFlip}
      className={cn(
        "relative h-6 w-[42px] shrink-0 rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-60",
        "focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
        checked ? "bg-label-1" : "border border-line-strong bg-surface",
      )}
    >
      <span
        className={cn(
          "absolute top-[3px] left-[3px] h-[18px] w-[18px] rounded-full transition-transform",
          checked ? "translate-x-[18px] bg-surface" : "bg-line-strong",
        )}
      />
    </button>
  );
}

function DeleteButton({ onDelete }: { onDelete: () => void }): ReactElement {
  return (
    <button
      type="button"
      aria-label="Delete schedule"
      onClick={onDelete}
      className="shrink-0 rounded-md p-1.5 text-label-3 transition-colors hover:bg-warn-soft hover:text-warn-ink focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
    >
      <Trash2 size={14} />
    </button>
  );
}

// The one glance a card's top row gives beyond the cadence and last-run detail below it: a
// fire in flight beats everything else, a paused or waiting live run reads the same word every
// other screen gives that state, a failed or refused last tick reads as failed, a schedule that
// has never fired says so, and anything else reads as caught up.
function activePill(schedule: Schedule): { tone: PillTone; word: string } {
  if (schedule.live === "running") return { tone: "running", word: "Running now" };
  if (schedule.live === "paused") return RUN_STATE_PILL.paused;
  if (schedule.live === "waiting") return RUN_STATE_PILL.waiting;
  if (outcomeFailed(schedule.last_outcome)) return { tone: "failed", word: "Last run failed" };
  if (schedule.last_run_at === null) return { tone: "queued", word: "Never fired" };
  return { tone: "completed", word: "Caught up" };
}

function lastRunSummary(
  schedule: Schedule,
  now: number,
): { text: string; toneClass: string; Icon: typeof CheckCircle2 } {
  if (schedule.last_run_at === null) {
    return { text: "Never run.", toneClass: "text-label-3", Icon: MinusCircle };
  }
  if (outcomeFailed(schedule.last_outcome)) {
    return {
      text: `Last run failed ${formatRelative(schedule.last_run_at, now)}.`,
      toneClass: "text-warn-ink",
      Icon: XCircle,
    };
  }
  return {
    text: `Last run completed ${formatRelative(schedule.last_run_at, now)}.`,
    toneClass: "text-label-2",
    Icon: CheckCircle2,
  };
}

// A missing reading (no run of this schedule has been priced) reads the same as "0".
function spendReading(value: string | null | undefined): string {
  return value ?? "0";
}

// "Nothing spent this week." only when nothing was spent AND nothing went unpriced, otherwise both
// readings, today's first. A schedule whose every run ran on a model no source can price reads zero
// on both figures, and saying it spent nothing would be the plainest possible lie about its cost.
function spendSentence(schedule: Schedule): string {
  const today = spendReading(schedule.spend_today_usd);
  const week = spendReading(schedule.spend_week_usd);
  const todayUnknown = schedule.spend_today_unpriced ?? 0;
  const weekUnknown = schedule.spend_week_unpriced ?? 0;
  const nothingPriced = Number.parseFloat(today) === 0 && Number.parseFloat(week) === 0;
  if (nothingPriced && todayUnknown === 0 && weekUnknown === 0) return "Nothing spent this week.";
  return `Spent ${spendText(today, todayUnknown)} today, ${spendText(week, weekUnknown)} this week.`;
}

function pausedSentence(schedule: Schedule, now: number): string {
  const words = capitalize(cadenceWords(schedule.cadence));
  if (schedule.paused_at === null) {
    return `${words}, paused by a person. It will not fire while it is paused.`;
  }
  return `${words}, paused by a person, ${formatRelative(schedule.paused_at, now)}. It will not fire while it is paused.`;
}

export interface ScheduleCardProps {
  schedule: Schedule;
  name: string;
  togglingEnabled: boolean;
  runningNow: boolean;
  onToggleEnabled: () => void;
  onRunNow: () => void;
  onDelete: () => void;
  now?: number;
}

export function ScheduleCard({
  schedule,
  name,
  togglingEnabled,
  runningNow,
  onToggleEnabled,
  onRunNow,
  onDelete,
  now = Date.now(),
}: ScheduleCardProps): ReactElement {
  const router = useRouter();
  const href = `/workflows/${encodeURIComponent(schedule.workflow_id)}`;

  function guardedClick(event: MouseEvent<HTMLLIElement>) {
    if (event.target instanceof Element && event.target.closest("button, input, a, textarea")) {
      return;
    }
    router.push(href);
  }

  if (!schedule.enabled) {
    return (
      // biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it; the keyboard path is the workflow name's own link.
      <li
        onClick={guardedClick}
        className="cursor-pointer overflow-hidden rounded-lg border border-dashed border-line-strong bg-well transition-colors"
      >
        <div className="flex items-center gap-2.5 px-5 pt-4 pb-3.5">
          <ScheduleSwitch checked={false} busy={togglingEnabled} onFlip={onToggleEnabled} />
          <Link
            href={href}
            className="min-w-0 flex-1 truncate font-semibold text-label-2 text-subtitle hover:underline"
          >
            {name}
          </Link>
          <span className="shrink-0 rounded-full border border-line px-2.5 py-1 font-mono text-label-3 text-micro">
            {schedule.workflow_id}
          </span>
          <StatePill tone={RUN_STATE_PILL.paused.tone} size="sm">
            {RUN_STATE_PILL.paused.word}
          </StatePill>
          <DeleteButton onDelete={onDelete} />
        </div>
        <p className="px-5 pb-4 text-caption text-label-2">{pausedSentence(schedule, now)}</p>
      </li>
    );
  }

  const pill = activePill(schedule);
  const lastRun = lastRunSummary(schedule, now);
  const alreadyLive = schedule.live !== null;

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it; the keyboard path is the workflow name's own link.
    <li
      onClick={guardedClick}
      className="cursor-pointer overflow-hidden rounded-lg border border-line bg-surface shadow-sm transition-colors hover:border-line-strong"
    >
      <div className="flex items-center gap-2.5 px-5 pt-4 pb-3.5">
        <ScheduleSwitch checked busy={togglingEnabled} onFlip={onToggleEnabled} />
        <Link
          href={href}
          className="min-w-0 flex-1 truncate font-semibold text-label-1 text-subtitle hover:underline"
        >
          {name}
        </Link>
        <span className="shrink-0 rounded-full border border-line px-2.5 py-1 font-mono text-label-3 text-micro">
          {schedule.workflow_id}
        </span>
        <StatePill tone={pill.tone} size="sm">
          {pill.word}
        </StatePill>
        <DeleteButton onDelete={onDelete} />
      </div>

      <div className="flex flex-wrap items-center gap-2 px-5 pb-4">
        <MonoTag>{schedule.cadence}</MonoTag>
        <p className="text-caption text-label-2">
          Next run {formatNextRun(schedule.next_run_at, now)}.
        </p>
        <p className="text-caption text-label-2">{spendSentence(schedule)}</p>
        {propertyTags(schedule).map((tag) => (
          <MonoTag key={tag}>{tag}</MonoTag>
        ))}
      </div>

      <div className="flex items-center justify-between gap-3 border-line border-t bg-well px-5 py-3">
        <span className={cn("flex items-center gap-1.5 text-caption", lastRun.toneClass)}>
          <lastRun.Icon aria-hidden size={14} className="shrink-0" />
          {lastRun.text}
        </span>
        {alreadyLive ? (
          <OpenRunLink href={href} size="sm" />
        ) : (
          <Button onClick={onRunNow} disabled={runningNow} className="h-7 px-3 text-caption">
            {runningNow ? "Running" : "Run now"}
          </Button>
        )}
      </div>
    </li>
  );
}
