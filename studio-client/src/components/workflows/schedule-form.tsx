"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { useEffect, useId, useMemo, useState } from "react";

import { knowledgeBaseApi } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";
import { type Schedule, type ScheduleUpdateInput, schedulesApi } from "@/api/schedules";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { GateMode, JsonValue } from "@/api/studio-types";
import {
  type KnowledgeBaseRegistry,
  type ParameterDraft,
  ParameterFields,
  draftFrom,
  emptyDraft,
  parameterFields,
  readParameters,
  usesKnowledgeBases,
} from "@/components/workflows/parameter-fields";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Select } from "@/shared/ui/select";

// The one route onto a new schedule, and onto a change to an existing one. Creating, it picks a
// workflow (fixed, when the caller already knows which one, a select fed by `studioApi.library()`
// otherwise). Editing, it opens on the schedule's own values and its workflow cannot change. Either
// way it answers the workflow's declared parameters through the same field renderer a run is set
// up with (fetched via `studioApi.workflow`, the same route `RunControls` reads), names a cadence
// (an interval, a cron expression or an event) with a live preview, and chooses how its gates
// behave unattended. An edit hands back only the fields that changed.

type CadenceMode = "interval" | "cron" | "event";
type IntervalUnit = "m" | "h" | "d";
type GatePolicyMode = "wait" | "fail_after";

const UNIT_LABEL: Record<IntervalUnit, string> = { m: "minutes", h: "hours", d: "days" };

// Every field the form holds besides the arguments, as typed.
interface FormValues {
  cadenceMode: CadenceMode;
  intervalCount: string;
  intervalUnit: IntervalUnit;
  cronText: string;
  eventText: string;
  gatePolicyMode: GatePolicyMode;
  failAfterSeconds: string;
  gateMode: GateMode;
  capacityKey: string;
  capacityLimit: string;
  blackout: string;
  catchUp: boolean;
}

const BLANK_VALUES: FormValues = {
  cadenceMode: "interval",
  intervalCount: "30",
  intervalUnit: "m",
  cronText: "",
  eventText: "",
  gatePolicyMode: "wait",
  failAfterSeconds: "300",
  gateMode: "wait",
  capacityKey: "",
  capacityLimit: "",
  blackout: "",
  catchUp: false,
};

// The interval fields hold minutes, hours and days. An interval in seconds is kept as written.
const INTERVAL = /^every\s+(\d+)\s*([mhd])$/i;
const ON_REFRESH = /^on\s+refresh$/i;
const AFTER_RUN = /^after\s+\S+$/i;
const ON_INDEX = /^on\s+index\s+\S+(?:\s+quiet\s+\d+\s*[smh])?$/i;
const EVENT_CADENCES = [ON_REFRESH, AFTER_RUN, ON_INDEX];
// Checked after the event cadences, since "on index docs quiet 30m" also has five words.
const CRON = /^\S+(?:\s+\S+){4}$/;
const FAIL_AFTER = /^fail_after:(.*)$/;

function isEventCadence(text: string): boolean {
  return EVENT_CADENCES.some((pattern) => pattern.test(text));
}

// A stored cadence split back into the fields that show it. Anything the interval and cron fields
// cannot show lands in the event box exactly as written.
function cadenceValues(cadence: string): Partial<FormValues> {
  const text = cadence.trim();
  const interval = INTERVAL.exec(text);
  if (interval !== null) {
    const unit = interval[2].toLowerCase();
    return {
      cadenceMode: "interval",
      intervalCount: interval[1],
      intervalUnit: unit === "h" ? "h" : unit === "d" ? "d" : "m",
    };
  }
  if (!isEventCadence(text) && CRON.test(text)) return { cadenceMode: "cron", cronText: text };
  return { cadenceMode: "event", eventText: text };
}

function valuesFrom(schedule: Schedule): FormValues {
  const failAfter = FAIL_AFTER.exec(schedule.gate_policy);
  return {
    ...BLANK_VALUES,
    ...cadenceValues(schedule.cadence),
    gatePolicyMode: failAfter === null ? "wait" : "fail_after",
    failAfterSeconds: failAfter === null ? BLANK_VALUES.failAfterSeconds : failAfter[1],
    gateMode: schedule.gate_mode,
    capacityKey: schedule.capacity_key ?? "",
    capacityLimit: schedule.capacity_limit === null ? "" : String(schedule.capacity_limit),
    blackout: schedule.blackout ?? "",
    catchUp: schedule.catch_up,
  };
}

function composeCadence(values: FormValues): string {
  if (values.cadenceMode === "cron") return values.cronText.trim();
  if (values.cadenceMode === "event") return values.eventText.trim();
  const trimmed = values.intervalCount.trim();
  const n = Number(trimmed);
  if (trimmed === "" || !Number.isFinite(n) || n <= 0) return "";
  return `every ${trimmed}${values.intervalUnit}`;
}

function composeGatePolicy(mode: GatePolicyMode, failAfterSeconds: string): string {
  if (mode === "wait") return "wait";
  return `fail_after:${failAfterSeconds.trim()}`;
}

function composePayload(
  workflowId: string,
  argumentValues: Record<string, JsonValue>,
  values: FormValues,
): ScheduleFormPayload {
  const trimmedCapacityKey = values.capacityKey.trim();
  const trimmedCapacityLimit = values.capacityLimit.trim();
  const parsedCapacityLimit = Number(trimmedCapacityLimit);
  const trimmedBlackout = values.blackout.trim();
  return {
    workflowId,
    arguments: argumentValues,
    cadence: composeCadence(values),
    gatePolicy: composeGatePolicy(values.gatePolicyMode, values.failAfterSeconds),
    gateMode: values.gateMode,
    capacityKey: trimmedCapacityKey === "" ? null : trimmedCapacityKey,
    capacityLimit:
      trimmedCapacityKey === "" ||
      trimmedCapacityLimit === "" ||
      !Number.isFinite(parsedCapacityLimit)
        ? null
        : parsedCapacityLimit,
    blackout: trimmedBlackout === "" ? null : trimmedBlackout,
    catchUp: values.catchUp,
  };
}

// The fields `current` changes from `opened`, both composed the same way, so a value the form only
// rewrote (such as "EVERY 30M" reopened as "every 30m") is never sent. Arguments go whole, since a
// change replaces every stored argument. An emptied field goes as null, which clears it.
function scheduleChanges(
  opened: ScheduleFormPayload,
  current: ScheduleFormPayload,
): ScheduleUpdateInput {
  const changes: ScheduleUpdateInput = {};
  if (JSON.stringify(current.arguments) !== JSON.stringify(opened.arguments)) {
    changes.arguments = current.arguments;
  }
  if (current.cadence !== opened.cadence) changes.cadence = current.cadence;
  if (current.gatePolicy !== opened.gatePolicy) changes.gate_policy = current.gatePolicy;
  if (current.gateMode !== opened.gateMode) changes.gate_mode = current.gateMode;
  if (current.capacityKey !== opened.capacityKey) changes.capacity_key = current.capacityKey;
  if (current.capacityLimit !== opened.capacityLimit) {
    changes.capacity_limit = current.capacityLimit;
    // The backend clears a capacity key with its limit, so an emptied limit clears the key here too,
    // as the form says next to the field.
    if (current.capacityLimit === null) changes.capacity_key = null;
  }
  if (current.blackout !== opened.blackout) changes.blackout = current.blackout;
  if (current.catchUp !== opened.catchUp) changes.catch_up = current.catchUp;
  return changes;
}

const WHOLE_NUMBER = /^\d+$/;

// Why the capacity limit cannot be saved, or `null`. Only a limit beside a capacity key counts,
// since the limit field is off without one.
function capacityLimitError(capacityKey: string, capacityLimit: string): string | null {
  const limit = capacityLimit.trim();
  if (capacityKey.trim() === "" || limit === "") return null;
  if (WHOLE_NUMBER.test(limit) && Number(limit) > 0) return null;
  return "The capacity limit must be a whole number above zero.";
}

// A loose shape check for a nudge only. The backend's own refusal sentence (rendered through
// this form's `error` prop) is what actually decides whether a blackout parses.
const BLACKOUT_SHAPE = /^\d{2}:\d{2}-\d{2}:\d{2}(\s*,\s*\d{2}:\d{2}-\d{2}:\d{2})*$/;

function blackoutLooksOff(text: string): boolean {
  const trimmed = text.trim();
  return trimmed !== "" && !BLACKOUT_SHAPE.test(trimmed);
}

// The column head style every field label and fieldset legend in this form shares.
const FIELD_LABEL = "text-micro font-semibold uppercase tracking-caps text-label-3";

// One "Gate mode" radio card: a circular dot indicator plus its sentence, selected reading as an
// ink border and well fill, unselected as a plain line border. The native input stays in the tab
// order and the accessibility tree (visually hidden, not `hidden`), so its role and accessible
// name are exactly the sentence a caller passes as `label`; `has-[:focus-visible]` puts the ring
// on the visible card itself whenever that hidden input holds keyboard focus, so tabbing through
// the three options is never invisible.
function GateModeOption({
  name,
  checked,
  onChange,
  label,
}: {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: string;
}): ReactElement {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-center gap-2.5 rounded-md border p-2.5 transition-colors",
        "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-accent has-[:focus-visible]:ring-offset-2 has-[:focus-visible]:ring-offset-surface",
        checked ? "border-label-1 bg-well" : "border-line bg-surface hover:border-line-strong",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2",
          checked ? "border-label-1" : "border-line-strong",
        )}
      >
        {checked ? <span className="h-2 w-2 rounded-full bg-label-1" /> : null}
      </span>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="sr-only" />
      <span className={cn("text-body", checked ? "font-semibold text-label-1" : "text-label-2")}>
        {label}
      </span>
    </label>
  );
}

export interface ScheduleFormPayload {
  workflowId: string;
  // Typed exactly as collected: an array stays an array, a boolean a boolean.
  arguments: Record<string, JsonValue>;
  cadence: string;
  gatePolicy: string;
  // Who decides a gate this schedule's own run reaches, posted as `gate_mode`.
  gateMode: GateMode;
  capacityKey: string | null;
  capacityLimit: number | null;
  blackout: string | null;
  catchUp: boolean;
}

interface ScheduleFormCommonProps {
  open: boolean;
  submitting: boolean;
  error?: string | null;
  onCancel: () => void;
}

export interface NewScheduleFormProps extends ScheduleFormCommonProps {
  // Preselected when opened from a workflow's own page. Absent when opened standalone from the
  // schedules station, in which case a select (fed by `studioApi.library()`) lets a person choose
  // one here.
  workflowId?: string;
  schedule?: undefined;
  onSubmit: (payload: ScheduleFormPayload) => void;
}

export interface EditScheduleFormProps extends ScheduleFormCommonProps {
  // The schedule as it stood when its edit began. The form opens on its values, and its workflow
  // is shown but cannot change.
  schedule: Schedule;
  workflowId?: undefined;
  // Called with only the fields that changed. Saving is off until something has.
  onSave: (changes: ScheduleUpdateInput) => void;
}

export type ScheduleFormProps = NewScheduleFormProps | EditScheduleFormProps;

export function ScheduleForm(props: ScheduleFormProps) {
  const { open, submitting, error, onCancel, schedule } = props;
  const editing = schedule !== undefined;
  const workflowId = schedule?.workflow_id ?? props.workflowId;
  const titleId = useId();
  const workflowSelectId = useId();
  const capacityKeyId = useId();
  const capacityLimitId = useId();
  const capacityNoteId = useId();
  const blackoutId = useId();
  const standalone = workflowId === undefined;

  // What every field opens on: the schedule's own values when editing, the defaults otherwise.
  const opening = useMemo(
    () => (schedule === undefined ? BLANK_VALUES : valuesFrom(schedule)),
    [schedule],
  );

  const [pickedWorkflowId, setPickedWorkflowId] = useState("");
  const [cadenceMode, setCadenceMode] = useState<CadenceMode>(opening.cadenceMode);
  const [intervalCount, setIntervalCount] = useState(opening.intervalCount);
  const [intervalUnit, setIntervalUnit] = useState<IntervalUnit>(opening.intervalUnit);
  const [cronText, setCronText] = useState(opening.cronText);
  const [eventText, setEventText] = useState(opening.eventText);
  const [gatePolicyMode, setGatePolicyMode] = useState<GatePolicyMode>(opening.gatePolicyMode);
  const [failAfterSeconds, setFailAfterSeconds] = useState(opening.failAfterSeconds);
  const [gateMode, setGateMode] = useState<GateMode>(opening.gateMode);
  const [capacityKey, setCapacityKey] = useState(opening.capacityKey);
  const [capacityLimit, setCapacityLimit] = useState(opening.capacityLimit);
  const [blackout, setBlackout] = useState(opening.blackout);
  const [catchUp, setCatchUp] = useState(opening.catchUp);
  const [draft, setDraft] = useState<ParameterDraft>({});
  const [showErrors, setShowErrors] = useState(false);
  const [preview, setPreview] = useState<{ occurrences: string[] | null; error: string | null }>({
    occurrences: null,
    error: null,
  });

  const effectiveWorkflowId = workflowId ?? pickedWorkflowId;
  const values: FormValues = {
    cadenceMode,
    intervalCount,
    intervalUnit,
    cronText,
    eventText,
    gatePolicyMode,
    failAfterSeconds,
    gateMode,
    capacityKey,
    capacityLimit,
    blackout,
    catchUp,
  };
  const cadence = composeCadence(values);
  // A stored cadence the interval and cron fields cannot show, such as "every 45s", kept as written.
  const keptAsWritten =
    editing &&
    opening.cadenceMode === "event" &&
    !isEventCadence(opening.eventText) &&
    cadenceMode === "event" &&
    eventText === opening.eventText;

  // The form starts over every time the dialog opens. It stays mounted between opens, so nothing
  // typed for a discarded attempt lingers into the next one.
  useEffect(() => {
    if (!open) return;
    setPickedWorkflowId("");
    setCadenceMode(opening.cadenceMode);
    setIntervalCount(opening.intervalCount);
    setIntervalUnit(opening.intervalUnit);
    setCronText(opening.cronText);
    setEventText(opening.eventText);
    setGatePolicyMode(opening.gatePolicyMode);
    setFailAfterSeconds(opening.failAfterSeconds);
    setGateMode(opening.gateMode);
    setCapacityKey(opening.capacityKey);
    setCapacityLimit(opening.capacityLimit);
    setBlackout(opening.blackout);
    setCatchUp(opening.catchUp);
    setPreview({ occurrences: null, error: null });
  }, [open, opening]);

  useEffect(() => {
    if (!open) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onCancel]);

  // The next three fires this cadence would produce, live, debounced so every keystroke doesn't
  // round-trip. Empty (an unparsable interval count, or nothing typed yet) skips the call rather
  // than previewing a cadence nobody asked for.
  useEffect(() => {
    if (!open || cadence === "") {
      setPreview({ occurrences: null, error: null });
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      schedulesApi
        .preview(cadence)
        .then((occurrences) => {
          if (!cancelled) setPreview({ occurrences, error: null });
        })
        .catch((previewError: unknown) => {
          if (!cancelled)
            setPreview({ occurrences: null, error: studioErrorMessage(previewError) });
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, cadence]);

  const libraryQuery = useQuery({
    queryKey: ["library"],
    queryFn: () => studioApi.library(),
    enabled: open && standalone,
  });

  const workflowQuery = useQuery({
    queryKey: ["workflow", effectiveWorkflowId],
    queryFn: () => studioApi.workflow(effectiveWorkflowId),
    enabled: open && effectiveWorkflowId !== "",
  });

  const workflow = workflowQuery.data ?? null;
  const fields = useMemo(
    () => (workflow === null ? [] : parameterFields(workflow.graph, workflow.presentation)),
    [workflow],
  );
  // The questions this schedule is answering, and whose they are. A different workflow is a
  // different draft even when it declares the same questions; the same one left alone keeps
  // what was typed.
  const fieldShape = useMemo(
    () =>
      [effectiveWorkflowId, ...fields.map((field) => `${field.key}:${field.control}`)].join("|"),
    [effectiveWorkflowId, fields],
  );
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the field shape, never on the identity of an array a fetch re-derives.
  useEffect(() => {
    setDraft(schedule === undefined ? emptyDraft(fields) : draftFrom(fields, schedule.arguments));
    setShowErrors(false);
  }, [open, fieldShape]);

  const registry = useQuery({
    queryKey: knowledgeBaseKeys.all,
    queryFn: () => knowledgeBaseApi.knowledgeBases(),
    enabled: open && usesKnowledgeBases(fields),
  });
  // Unknown until the registry answers: a selector that has not been told what exists says so,
  // rather than reading as a deployment with no knowledge base at all.
  const knowledgeBases: KnowledgeBaseRegistry = {
    names:
      registry.data === undefined ? null : registry.data.knowledge_bases.map((base) => base.name),
    error: registry.isError ? studioErrorMessage(registry.error) : null,
  };
  const read = readParameters(fields, draft, { knowledgeBases });
  const unanswered = Object.keys(read.errors).length;
  const payload = composePayload(effectiveWorkflowId, read.values, values);
  // The schedule as the form opened on it, composed the same way as what is typed now.
  const opened =
    schedule === undefined
      ? null
      : composePayload(
          schedule.workflow_id,
          readParameters(fields, draftFrom(fields, schedule.arguments), { knowledgeBases }).values,
          opening,
        );
  const changes = opened === null ? null : scheduleChanges(opened, payload);
  const nothingChanged = changes !== null && Object.keys(changes).length === 0;
  const limitError = capacityLimitError(capacityKey, capacityLimit);
  // A saved limit emptied while the key stays, which clears the key as well on save.
  const limitClearsKey =
    opened !== null &&
    opened.capacityLimit !== null &&
    capacityKey.trim() !== "" &&
    capacityLimit.trim() === "";
  // An on refresh or after cadence fires only over a knowledge base the schedule's arguments choose,
  // so on a workflow that asks for none it would never fire.
  const firesNever =
    workflowQuery.data !== undefined &&
    cadenceMode === "event" &&
    (ON_REFRESH.test(cadence) || AFTER_RUN.test(cadence)) &&
    !usesKnowledgeBases(fields);

  if (!open) return null;

  const canSubmit =
    effectiveWorkflowId !== "" &&
    cadence !== "" &&
    !submitting &&
    !nothingChanged &&
    limitError === null;

  function submit() {
    if (!canSubmit) return;
    setShowErrors(true);
    if (unanswered > 0) return;
    if (props.schedule !== undefined) {
      if (changes !== null) props.onSave(changes);
      return;
    }
    props.onSubmit(payload);
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center"
    >
      <button
        type="button"
        aria-label="Dismiss dialog"
        tabIndex={-1}
        onClick={onCancel}
        className="absolute inset-0 cursor-default bg-black/55"
      />
      <div className="relative z-10 flex max-h-[85vh] w-full max-w-lg flex-col gap-3 overflow-y-auto rounded-lg bg-surface p-5 shadow-lg ring-1 ring-line">
        <h2 id={titleId} className="font-semibold text-label-1 text-subtitle">
          {editing ? "Edit schedule" : "New schedule"}
        </h2>

        {standalone ? (
          <label htmlFor={workflowSelectId} className={cn("flex flex-col gap-1", FIELD_LABEL)}>
            Workflow
            <Select
              id={workflowSelectId}
              value={pickedWorkflowId}
              onChange={(event) => setPickedWorkflowId(event.target.value)}
            >
              <option value="">Choose a workflow...</option>
              {(libraryQuery.data ?? []).map((card) => (
                <option key={card.id} value={card.id}>
                  {card.title}
                </option>
              ))}
            </Select>
          </label>
        ) : (
          <p className="flex flex-col gap-1">
            <span className={FIELD_LABEL}>Workflow</span>
            <span className="font-mono text-body text-label-1">{workflowId}</span>
          </p>
        )}

        {fields.length > 0 ? (
          <fieldset className="flex flex-col gap-1.5">
            <legend className={FIELD_LABEL}>Arguments</legend>
            <ParameterFields
              fields={fields}
              draft={draft}
              errors={showErrors ? read.errors : {}}
              knowledgeBases={knowledgeBases}
              disabled={submitting}
              onChange={(key, value) => setDraft((current) => ({ ...current, [key]: value }))}
            />
          </fieldset>
        ) : null}

        <fieldset className="flex flex-col gap-1.5">
          <legend className={FIELD_LABEL}>Cadence</legend>
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border p-2.5 text-body transition-colors",
              cadenceMode === "interval"
                ? "border-label-1 bg-well text-label-1"
                : "border-line bg-surface text-label-2 hover:border-line-strong",
            )}
          >
            <input
              type="radio"
              name="cadence-mode"
              checked={cadenceMode === "interval"}
              onChange={() => setCadenceMode("interval")}
              className="accent-[var(--label-1)]"
            />
            Every
            <Input
              aria-label="Interval count"
              inputMode="numeric"
              value={intervalCount}
              onChange={(event) => setIntervalCount(event.target.value)}
              disabled={cadenceMode !== "interval"}
              className="w-16"
            />
            <Select
              aria-label="Interval unit"
              value={intervalUnit}
              onChange={(event) => setIntervalUnit(event.target.value as IntervalUnit)}
              disabled={cadenceMode !== "interval"}
            >
              {Object.entries(UNIT_LABEL).map(([unit, label]) => (
                <option key={unit} value={unit}>
                  {label}
                </option>
              ))}
            </Select>
          </label>
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border p-2.5 text-body transition-colors",
              cadenceMode === "cron"
                ? "border-label-1 bg-well text-label-1"
                : "border-line bg-surface text-label-2 hover:border-line-strong",
            )}
          >
            <input
              type="radio"
              name="cadence-mode"
              checked={cadenceMode === "cron"}
              onChange={() => setCadenceMode("cron")}
              className="accent-[var(--label-1)]"
            />
            Cron expression
            <Input
              aria-label="Cron expression"
              value={cronText}
              onChange={(event) => setCronText(event.target.value)}
              disabled={cadenceMode !== "cron"}
              placeholder="0 9 * * 1"
              className="flex-1 font-mono"
            />
          </label>
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border p-2.5 text-body transition-colors",
              cadenceMode === "event"
                ? "border-label-1 bg-well text-label-1"
                : "border-line bg-surface text-label-2 hover:border-line-strong",
            )}
          >
            <input
              type="radio"
              name="cadence-mode"
              checked={cadenceMode === "event"}
              onChange={() => setCadenceMode("event")}
              className="accent-[var(--label-1)]"
            />
            On an event
            <Input
              aria-label="Event"
              value={eventText}
              onChange={(event) => setEventText(event.target.value)}
              disabled={cadenceMode !== "event"}
              placeholder="on refresh"
              className="flex-1 font-mono"
            />
          </label>
          {cadenceMode === "event" ? (
            <ul className="flex flex-col gap-0.5 text-label-3 text-micro">
              <li>
                <code className="font-mono">on refresh</code> runs after a refresh brings new
                sources into a knowledge base this schedule&apos;s arguments choose.
              </li>
              <li>
                <code className="font-mono">after &lt;workflow name&gt;</code> runs after a run of
                that workflow succeeds, fully or in part, over a knowledge base this schedule&apos;s
                arguments also choose.
              </li>
              <li>
                <code className="font-mono">on index &lt;knowledge base&gt; quiet 30m</code> runs
                once that knowledge base is indexed with changed pages and 30 minutes pass with no
                further index.
              </li>
            </ul>
          ) : null}
          {firesNever ? (
            <p className="text-micro text-warn-ink">
              This workflow does not ask for a knowledge base, so this event will never run it.
            </p>
          ) : null}
          {keptAsWritten ? (
            <p className="text-label-3 text-micro">
              The other fields cannot show this cadence, so it is kept here as written.
            </p>
          ) : null}
        </fieldset>

        {preview.error ? (
          <p role="alert" className="text-caption text-danger-ink">
            {preview.error}
          </p>
        ) : preview.occurrences?.length === 0 ? (
          <p className="text-caption text-label-3">
            Waits for that event instead of running at set times.
          </p>
        ) : preview.occurrences ? (
          <p className="text-caption text-label-3">
            Next three fires,{" "}
            {preview.occurrences.map((iso) => new Date(iso).toLocaleString()).join(", ")}.
          </p>
        ) : null}

        <fieldset className="flex flex-col gap-1.5">
          <legend className={FIELD_LABEL}>When a gate is reached</legend>
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border p-2.5 text-body transition-colors",
              gatePolicyMode === "wait"
                ? "border-label-1 bg-well text-label-1"
                : "border-line bg-surface text-label-2 hover:border-line-strong",
            )}
          >
            <input
              type="radio"
              name="gate-wait-policy"
              checked={gatePolicyMode === "wait"}
              onChange={() => setGatePolicyMode("wait")}
              className="accent-[var(--label-1)]"
            />
            Wait for a decision
          </label>
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border p-2.5 text-body transition-colors",
              gatePolicyMode === "fail_after"
                ? "border-label-1 bg-well text-label-1"
                : "border-line bg-surface text-label-2 hover:border-line-strong",
            )}
          >
            <input
              type="radio"
              name="gate-wait-policy"
              checked={gatePolicyMode === "fail_after"}
              onChange={() => setGatePolicyMode("fail_after")}
              className="accent-[var(--label-1)]"
            />
            Fail after
            <Input
              aria-label="Seconds before failing"
              inputMode="numeric"
              value={failAfterSeconds}
              onChange={(event) => setFailAfterSeconds(event.target.value)}
              disabled={gatePolicyMode !== "fail_after"}
              className="w-20"
            />
            seconds
          </label>
        </fieldset>

        <fieldset className="flex flex-col gap-1.5">
          <legend className={FIELD_LABEL}>Gates on runs from this schedule</legend>
          <GateModeOption
            name="schedule-gate-mode"
            checked={gateMode === "wait"}
            onChange={() => setGateMode("wait")}
            label="Wait for me"
          />
          <GateModeOption
            name="schedule-gate-mode"
            checked={gateMode === "orchestrate"}
            onChange={() => setGateMode("orchestrate")}
            label="The orchestrator decides"
          />
          <GateModeOption
            name="schedule-gate-mode"
            checked={gateMode === "auto"}
            onChange={() => setGateMode("auto")}
            label="Pass automatically (recorded as automatic)"
          />
        </fieldset>

        <details className="border-line border-t pt-2">
          <summary className={cn("cursor-pointer select-none", FIELD_LABEL)}>Advanced</summary>
          <div className="mt-1.5 flex flex-col gap-2">
            <div className="flex items-end gap-2">
              <label htmlFor={capacityKeyId} className="flex flex-1 flex-col gap-0.5">
                <span className={FIELD_LABEL}>Capacity key</span>
                <Input
                  id={capacityKeyId}
                  value={capacityKey}
                  onChange={(event) => setCapacityKey(event.target.value)}
                  placeholder="e.g. gpu-pool"
                />
              </label>
              <label htmlFor={capacityLimitId} className="flex flex-col gap-0.5">
                <span className={FIELD_LABEL}>Capacity limit</span>
                <Input
                  id={capacityLimitId}
                  inputMode="numeric"
                  value={capacityLimit}
                  onChange={(event) => setCapacityLimit(event.target.value)}
                  disabled={capacityKey.trim() === ""}
                  aria-invalid={limitError !== null}
                  aria-describedby={
                    limitError !== null || limitClearsKey ? capacityNoteId : undefined
                  }
                  className="w-20"
                />
              </label>
            </div>
            {limitError !== null ? (
              <p id={capacityNoteId} className="text-danger-ink text-micro">
                {limitError}
              </p>
            ) : limitClearsKey ? (
              <p id={capacityNoteId} className="text-micro text-warn-ink">
                With no limit, saving clears the capacity key too.
              </p>
            ) : null}
            <label htmlFor={blackoutId} className="flex flex-col gap-0.5">
              <span className={FIELD_LABEL}>Blackout windows</span>
              <Input
                id={blackoutId}
                value={blackout}
                onChange={(event) => setBlackout(event.target.value)}
                placeholder="22:00-06:00, 12:00-13:00"
                className="font-mono"
              />
            </label>
            <p className="text-label-3 text-micro">
              Format, HH:MM-HH:MM, comma-separated (e.g. 22:00-06:00, 12:00-13:00).
            </p>
            {blackoutLooksOff(blackout) ? (
              <p className="text-micro text-warn-ink">
                This does not look like a blackout window yet.
              </p>
            ) : null}
            <label className="flex items-center gap-2 text-body text-label-2">
              <input
                type="checkbox"
                checked={catchUp}
                onChange={(event) => setCatchUp(event.target.checked)}
              />
              Catch up a missed fire when possible
            </label>
          </div>
        </details>

        {showErrors && unanswered > 0 ? (
          <p role="alert" className="text-caption text-danger-ink">
            {unanswered === 1 ? "1 field needs an answer." : `${unanswered} fields need an answer.`}
          </p>
        ) : null}

        {error ? (
          <p role="alert" className="text-caption text-danger-ink">
            {error}
          </p>
        ) : null}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!canSubmit}>
            {editing
              ? submitting
                ? "Saving..."
                : "Save changes"
              : submitting
                ? "Creating..."
                : "Create"}
          </Button>
        </div>
      </div>
    </div>
  );
}
