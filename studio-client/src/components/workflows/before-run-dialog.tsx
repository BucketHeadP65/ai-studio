"use client";

import { useQuery } from "@tanstack/react-query";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Modal, ModalDescription, ModalTitle } from "@/shared/ui/modal";
import type { ReactElement } from "react";
import { useEffect, useId, useMemo, useState } from "react";

import { knowledgeBaseApi } from "@/api/knowledge-base";
import { knowledgeBaseKeys, studioKeys } from "@/api/query-keys";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { GateMode, JsonValue, MoneyBrake, RunIntent, WorkflowDetail } from "@/api/studio-types";
import { GATE_MODE_WORDS } from "@/components/marks/gate-mode";
import {
  type KnowledgeBaseRegistry,
  type ParameterDraft,
  type ParameterField,
  ParameterFields,
  draftFrom,
  emptyDraft,
  parameterFields,
  readParameters,
  usesKnowledgeBases,
} from "@/components/workflows/parameter-fields";
import { RadioCardGroup, type RadioCardOption } from "@/components/workflows/radio-card";

// The one setup a run passes through, wherever it is started from: what this run is called, a
// control per declared parameter, and who decides the gates it will reach. It resolves a
// `RunIntent` and hands it to the caller; the caller owns the session it then creates. So a
// cancelled setup, an Escape, and a submit with a field still wrong all leave the runner with
// nothing to undo.
//
// A refusal from the backend comes back through `error` and is shown here with everything typed
// still in place, since the values are exactly what needs correcting. A start that got no answer
// at all comes back through `unknownReason` and is shown the same way: the values stay, nothing is
// sent again on its own, and the one act offered is to go and read whether a run was made.
//
// What the run will be held to is read from the deployment itself and shown before the button: the
// money ceilings it mounts, and who decides the gates. A deployment that configured no ceiling
// says exactly that. Nothing here turns an absent brake into a figure.

const GATE_MODE_OPTIONS: readonly RadioCardOption<GateMode>[] = (
  ["wait", "orchestrate", "auto"] as const
).map((value) => ({
  value,
  label: GATE_MODE_WORDS[value].label,
  description: GATE_MODE_WORDS[value].description,
}));

/** How much of an input's own value a composed run title carries before it stops being a name. */
const TITLE_TAIL = 60;

/** The bound the launch route holds a run's own name to, so the box refuses what the route would. */
const RUN_TITLE_MAX = 200;

/** What the gate choice sits on until this deployment has said what its own default is. */
const FALLBACK_GATE_MODE: GateMode = "wait";

/**
 * What a preview opens holding: a previous run's own answers, every one still editable.
 *
 * `gateMode` is optional because a run's record does not keep the mode it ran under. A rerun that
 * cannot read one asks the question fresh, under this deployment's own default, rather than
 * claiming an answer nobody gave.
 */
export interface RunSetup {
  workflowId: string;
  runTitle: string;
  arguments: Record<string, JsonValue>;
  gateMode?: GateMode;
}

const WINDOW_WORDS: Record<MoneyBrake["window"], string> = {
  run: "per run",
  conversation: "per conversation",
  day: "per day",
  month: "per month",
  total: "in total",
};

/** One ceiling as a person reads it: the figure, and what it is counted over. */
function brakeText(brake: MoneyBrake): string {
  const warning =
    brake.warn_at === null || brake.warn_at === undefined
      ? ""
      : `, warned at ${Math.round(brake.warn_at * 100)}%`;
  return `$${brake.usd} ${WINDOW_WORDS[brake.window]}${warning}`;
}

// Only a declared gate makes the question worth asking: a workflow with none reaches nothing for
// anyone to decide.
function hasGate(detail: WorkflowDetail): boolean {
  return detail.graph.nodes.some(
    (node) =>
      node.kind === "gate" || node.kind === "judge" || node.kind === "orchestrator-gate",
  );
}

/** The run's own name, from the card and the first input the author put words to. */
function composeRunTitle(
  workflowTitle: string,
  firstInputKey: string | null,
  draft: ParameterDraft,
): string {
  if (firstInputKey === null) return workflowTitle;
  const held = draft[firstInputKey];
  const text = typeof held === "string" ? held.trim() : "";
  if (text === "") return workflowTitle;
  return `${workflowTitle}, ${text.slice(0, TITLE_TAIL)}`;
}

export interface BeforeRunDialogProps {
  open: boolean;
  workflowId: string;
  /** The card's own title, which names the run and the session the run lives in. */
  workflowTitle: string;
  /** The saved workflow when the caller already holds it; absent fetches it. */
  detail?: WorkflowDetail | null;
  /** True while the caller's own launch is in flight. */
  submitting?: boolean;
  /** The backend's refusal sentence, kept on screen with the values that caused it. */
  error?: string | null;
  /** A previous run's own setup, so a rerun opens on those values with every one still editable. */
  seed?: RunSetup | null;
  /** Why the last start's outcome is unknown, or `null`. Shown with the values still in place. */
  unknownReason?: string | null;
  /** Go and read whether that start made a run. Never sends the start again. */
  onReconcile?: () => void;
  reconciling?: boolean;
  /** True once the reading has answered that the unknown start made no run at all. */
  settledAsNothing?: boolean;
  onConfirm: (intent: RunIntent) => void;
  onCancel: () => void;
}

export function BeforeRunDialog({
  open,
  workflowId,
  workflowTitle,
  detail = null,
  submitting = false,
  error = null,
  seed = null,
  unknownReason = null,
  onReconcile,
  reconciling = false,
  settledAsNothing = false,
  onConfirm,
  onCancel,
}: BeforeRunDialogProps): ReactElement {
  const titleFieldId = useId();

  const fetched = useQuery({
    queryKey: ["studio", "workflow", workflowId],
    queryFn: () => studioApi.workflow(workflowId),
    enabled: open && detail === null,
  });
  const held = detail ?? fetched.data ?? null;

  const fields = useMemo<ParameterField[]>(
    () => (held === null ? [] : parameterFields(held.graph, held.presentation)),
    [held],
  );
  const firstInputKey = held?.presentation?.inputs[0]?.key ?? null;

  const registry = useQuery({
    queryKey: knowledgeBaseKeys.all,
    queryFn: () => knowledgeBaseApi.knowledgeBases(),
    enabled: open && usesKnowledgeBases(fields),
  });
  // Unknown until the registry answers: a selector that has not been told what exists says so,
  // rather than reading as a deployment with no knowledge base at all.
  const knowledgeBases = useMemo<KnowledgeBaseRegistry>(
    () => ({
      names:
        registry.data === undefined
          ? null
          : registry.data.knowledge_bases.map((base) => base.name),
      error: registry.isError ? studioErrorMessage(registry.error) : null,
    }),
    [registry.data, registry.isError, registry.error],
  );

  const limits = useQuery({
    queryKey: studioKeys.runLimits,
    queryFn: () => studioApi.runLimits(),
    enabled: open,
  });
  const gateModeOptions = GATE_MODE_OPTIONS.filter(
    (option) => limits.data === undefined || limits.data.gate_modes.includes(option.value),
  );

  const [draft, setDraft] = useState<ParameterDraft>({});
  // Only what the person picked in this dialog. What they picked nothing on is answered by the
  // seed, then by the deployment's own default, so a default read a moment late still lands.
  const [pickedGateMode, setPickedGateMode] = useState<GateMode | null>(null);
  const [typedTitle, setTypedTitle] = useState("");
  const [titleTouched, setTitleTouched] = useState(false);
  const [showErrors, setShowErrors] = useState(false);

  // What this setup is actually asking for. A caller that rebuilds its `detail` object on every
  // render hands us a new `fields` array each time, so the reset below turns on this instead:
  // the same questions are the same form, and what a person typed stays where they typed it.
  const fieldShape = useMemo(
    () => fields.map((field) => `${field.key}:${field.control}`).join("|"),
    [fields],
  );

  // What a rerun opens with, as one string, so a different previous run re-seeds the form and the
  // same one never does. `null` is an ordinary setup, which opens empty.
  const seedKey = useMemo(
    () => (seed === null ? "" : JSON.stringify([seed.arguments, seed.runTitle, seed.gateMode])),
    [seed],
  );

  // A fresh setup every time it opens, and again the moment the workflow it is setting up
  // actually lands: nothing typed for an abandoned attempt carries into the next one. Opened over
  // a previous run's own setup, those values are what it opens holding, every one still editable.
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the field shape and the seed, never on the identity of an array or an object a caller re-derives.
  useEffect(() => {
    if (!open) return;
    const opening = seed === null ? emptyDraft(fields) : draftFrom(fields, seed.arguments);
    setDraft(opening);
    setPickedGateMode(null);
    setTypedTitle(seed?.runTitle ?? "");
    // A name this dialog would compose itself is not one anybody chose, so it keeps composing as
    // the values change. A name somebody wrote is theirs, and a rerun opens still holding it.
    setTitleTouched(
      seed !== null &&
        seed.runTitle !== "" &&
        seed.runTitle !== composeRunTitle(workflowTitle, firstInputKey, opening),
    );
    setShowErrors(false);
  }, [open, fieldShape, seedKey]);

  const gateMode =
    pickedGateMode ?? seed?.gateMode ?? limits.data?.default_gate_mode ?? FALLBACK_GATE_MODE;
  const read = readParameters(fields, draft, { knowledgeBases });
  const unanswered = Object.keys(read.errors).length;
  const runTitle = titleTouched
    ? typedTitle
    : composeRunTitle(workflowTitle, firstInputKey, draft);

  function confirm(): void {
    setShowErrors(true);
    // The button is disabled while a start is in flight, and this says the same thing again where
    // the request is actually made: two presses landing inside one render are still one start.
    if (submitting || held === null || unanswered > 0) return;
    onConfirm({
      workflowId,
      runTitle: runTitle.trim(),
      arguments: read.values,
      gateMode,
    });
  }

  return (
    <Modal open={open} onClose={onCancel} className="max-w-lg">
      <div className="flex max-h-[80vh] flex-col gap-3 overflow-y-auto">
        <ModalTitle className="text-subtitle">Set up this run</ModalTitle>
        <ModalDescription className="mt-0 text-caption text-label-3">
          {held?.summary || "Fill in what this run needs, then start it."}
        </ModalDescription>

        {held === null ? (
          <p className="text-body text-label-3">
            {fetched.isError
              ? studioErrorMessage(fetched.error)
              : "Reading what this run needs."}
          </p>
        ) : (
          <>
            <div className="flex flex-col gap-1">
              <label
                htmlFor={titleFieldId}
                className="font-semibold text-label-3 text-micro uppercase tracking-caps"
              >
                Run title
              </label>
              <Input
                id={titleFieldId}
                value={runTitle}
                maxLength={RUN_TITLE_MAX}
                disabled={submitting}
                onChange={(event) => {
                  setTitleTouched(true);
                  setTypedTitle(event.target.value);
                }}
              />
            </div>

            <ParameterFields
              fields={fields}
              draft={draft}
              errors={showErrors ? read.errors : {}}
              knowledgeBases={knowledgeBases}
              disabled={submitting}
              onChange={(key, value) => setDraft((current) => ({ ...current, [key]: value }))}
            />

            {hasGate(held) && gateModeOptions.length > 0 ? (
              <RadioCardGroup
                name="before-run-gate-mode"
                legend="Gates on this run"
                value={gateMode}
                onChange={setPickedGateMode}
                options={gateModeOptions}
                disabled={submitting}
              />
            ) : null}

            <div className="flex flex-col gap-1" data-section="run-limits">
              <span className="font-semibold text-label-3 text-micro uppercase tracking-caps">
                What this run is held to
              </span>
              {limits.data === undefined ? (
                <p className="text-caption text-label-3">
                  {limits.isError
                    ? "The limits on this run could not be read."
                    : "Reading what this run is held to."}
                </p>
              ) : limits.data.brakes.length === 0 ? (
                <p className="text-caption text-label-3">
                  No limit configured. Nothing here stops this run on cost.
                </p>
              ) : (
                <ul className="flex flex-col gap-0.5">
                  {limits.data.brakes.map((brake) => (
                    <li key={`${brake.window}-${brake.usd}`} className="text-caption text-label-2">
                      {brakeText(brake)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}

        {showErrors && unanswered > 0 ? (
          <p role="alert" className="text-caption text-danger-ink">
            {unanswered === 1 ? "1 field needs an answer." : `${unanswered} fields need an answer.`}
          </p>
        ) : null}

        {error === null ? null : (
          <p role="alert" className="text-caption text-danger-ink">
            {error}
          </p>
        )}

        {unknownReason === null ? null : (
          <div className="flex flex-col gap-1.5" data-section="unknown-start">
            {settledAsNothing ? (
              <p role="status" className="text-caption text-label-2">
                No run was started. Your answers are still here, and starting is yours to do.
              </p>
            ) : (
              <p role="alert" className="text-caption text-warn-ink">
                This run may or may not have started. {unknownReason} Nothing was sent again.
              </p>
            )}
            {onReconcile === undefined ? null : (
              <div>
                <Button variant="secondary" onClick={onReconcile} disabled={reconciling}>
                  {reconciling ? "Checking" : "Check whether it started"}
                </Button>
              </div>
            )}
          </div>
        )}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
          <Button onClick={confirm} disabled={submitting || held === null}>
            {submitting ? "Starting" : unknownReason === null ? "Run it" : "Start it again"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
