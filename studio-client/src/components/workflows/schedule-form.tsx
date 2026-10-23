"use client";

import { useQuery } from "@tanstack/react-query";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Select } from "@/shared/ui/select";
import type { ReactElement } from "react";
import { useEffect, useId, useMemo, useState } from "react";

import { knowledgeBaseApi } from "@/api/knowledge-base";
import { knowledgeBaseKeys } from "@/api/query-keys";
import { schedulesApi } from "@/api/schedules";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { GateMode, JsonValue } from "@/api/studio-types";
import {
  type KnowledgeBaseRegistry,
  type ParameterDraft,
  ParameterFields,
  emptyDraft,
  parameterFields,
  readParameters,
  usesKnowledgeBases,
} from "@/components/workflows/parameter-fields";

// The one route onto a new schedule: pick a workflow (fixed, when the caller already knows which
// one; a select fed by `studioApi.library()` otherwise), answer its declared parameters through
// the same field renderer a run is set up with (fetched via `studioApi.workflow`, the same route
// `RunControls` reads), name a cadence with a live three-fire preview, and choose how its gates
// behave unattended. Editing an existing schedule is not this form's job (creation only). The
// lane's own switch, run-now, and delete cover the rest.

type CadenceMode = "interval" | "cron";
type IntervalUnit = "m" | "h" | "d";
type GatePolicyMode = "wait" | "fail_after";

const UNIT_LABEL: Record<IntervalUnit, string> = { m: "minutes", h: "hours", d: "days" };

function composeCadence(
  mode: CadenceMode,
  intervalCount: string,
  intervalUnit: IntervalUnit,
  cronText: string,
): string {
  if (mode === "cron") return cronText.trim();
  const trimmed = intervalCount.trim();
  const n = Number(trimmed);
  if (trimmed === "" || !Number.isFinite(n) || n <= 0) return "";
  return `every ${trimmed}${intervalUnit}`;
}

function composeGatePolicy(mode: GatePolicyMode, failAfterSeconds: string): string {
  if (mode === "wait") return "wait";
  return `fail_after:${failAfterSeconds.trim()}`;
}

// A loose shape check for a nudge only — the backend's own refusal sentence (rendered through
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

export interface ScheduleFormProps {
  open: boolean;
  // Preselected when opened from a workflow's own page; absent when opened standalone from the
  // fleet's Scheduled lane, in which case a select (fed by `studioApi.library()`) lets a person
  // choose one here.
  workflowId?: string;
  submitting: boolean;
  error?: string | null;
  onSubmit: (payload: ScheduleFormPayload) => void;
  onCancel: () => void;
}

export function ScheduleForm({
  open,
  workflowId,
  submitting,
  error,
  onSubmit,
  onCancel,
}: ScheduleFormProps) {
  const titleId = useId();
  const workflowSelectId = useId();
  const capacityKeyId = useId();
  const capacityLimitId = useId();
  const blackoutId = useId();
  const standalone = workflowId === undefined;

  const [pickedWorkflowId, setPickedWorkflowId] = useState("");
  const [cadenceMode, setCadenceMode] = useState<CadenceMode>("interval");
  const [intervalCount, setIntervalCount] = useState("30");
  const [intervalUnit, setIntervalUnit] = useState<IntervalUnit>("m");
  const [cronText, setCronText] = useState("");
  const [gatePolicyMode, setGatePolicyMode] = useState<GatePolicyMode>("wait");
  const [failAfterSeconds, setFailAfterSeconds] = useState("300");
  const [gateMode, setGateMode] = useState<GateMode>("wait");
  const [capacityKey, setCapacityKey] = useState("");
  const [capacityLimit, setCapacityLimit] = useState("");
  const [blackout, setBlackout] = useState("");
  const [catchUp, setCatchUp] = useState(false);
  const [draft, setDraft] = useState<ParameterDraft>({});
  const [showErrors, setShowErrors] = useState(false);
  const [preview, setPreview] = useState<{ occurrences: string[] | null; error: string | null }>({
    occurrences: null,
    error: null,
  });

  const effectiveWorkflowId = workflowId ?? pickedWorkflowId;
  const cadence = composeCadence(cadenceMode, intervalCount, intervalUnit, cronText);

  // A fresh form every time the dialog opens — it stays mounted between opens, so nothing typed
  // for a discarded attempt should linger into the next one.
  useEffect(() => {
    if (!open) return;
    setPickedWorkflowId("");
    setCadenceMode("interval");
    setIntervalCount("30");
    setIntervalUnit("m");
    setCronText("");
    setGatePolicyMode("wait");
    setFailAfterSeconds("300");
    setGateMode("wait");
    setCapacityKey("");
    setCapacityLimit("");
    setBlackout("");
    setCatchUp(false);
    setPreview({ occurrences: null, error: null });
  }, [open]);

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
    setDraft(emptyDraft(fields));
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

  if (!open) return null;

  const canSubmit = effectiveWorkflowId !== "" && cadence !== "" && !submitting;

  function submit() {
    if (!canSubmit) return;
    setShowErrors(true);
    if (unanswered > 0) return;
    const trimmedCapacityKey = capacityKey.trim();
    const trimmedCapacityLimit = capacityLimit.trim();
    const parsedCapacityLimit = Number(trimmedCapacityLimit);
    const trimmedBlackout = blackout.trim();
    onSubmit({
      workflowId: effectiveWorkflowId,
      arguments: read.values,
      cadence,
      gatePolicy: composeGatePolicy(gatePolicyMode, failAfterSeconds),
      gateMode,
      capacityKey: trimmedCapacityKey === "" ? null : trimmedCapacityKey,
      capacityLimit:
        trimmedCapacityKey === "" ||
        trimmedCapacityLimit === "" ||
        !Number.isFinite(parsedCapacityLimit)
          ? null
          : parsedCapacityLimit,
      blackout: trimmedBlackout === "" ? null : trimmedBlackout,
      catchUp,
    });
  }

  return (
    // biome-ignore lint/a11y/useSemanticElements: jsdom in tests doesn't implement <dialog>.showModal, matches the shared ConfirmDialog.
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
          New schedule
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
        </fieldset>

        {preview.error ? (
          <p role="alert" className="text-caption text-danger-ink">
            {preview.error}
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
                  className="w-20"
                />
              </label>
            </div>
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
            {submitting ? "Creating..." : "Create"}
          </Button>
        </div>
      </div>
    </div>
  );
}
