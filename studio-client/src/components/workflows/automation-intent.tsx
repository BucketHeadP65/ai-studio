"use client";

import { Button } from "client-core/ui/button";
import { Select } from "client-core/ui/select";
import { Textarea } from "client-core/ui/textarea";
import { Input } from "client-core/ui/input";
import type { ReactElement } from "react";
import { useEffect, useId, useState } from "react";

import Link from "next/link";

import type { IntentIssue, Turn, WorkflowIntent } from "@/api/studio-types";

// What a person asked for, beside the automation drafted to answer it.
//
// Two screens, in the order they are met. Before anything is drafted, one box asks for the task
// and nothing else: a person with a job to automate should not be met by a form. After a draft
// arrives, the same panel shows what the agent recorded, every expected answer visible whether or
// not it was answered, and every assumption it made on the person's behalf marked as inferred so
// they can say no to it.
//
// The `Inferred` badge marks the list, not each entry's history: an intent records that something
// was assumed on the person's behalf and not who last edited which line, so an assumption they
// have rewritten still reads as inferred. Dropping it is the way to say no to one.
//
// Between the two sits the exchange: what was said in this session so far, and a box to say what
// should change. Being shown a proposal and having nowhere to answer it is the one thing a design
// screen cannot do, so a second turn is part of the screen rather than somewhere else to navigate.
//
// Nothing here starts a run or creates a schedule. Recording a request is recording a request.
// The graph and its YAML live under Details on the page that holds this panel, because they are
// the answer to the question, not the question.

/** The four answers an intent is asked for, each under the words it is asked in. */
const EXPECTED: readonly { key: IntentIssue; label: string }[] = [
  { key: "expected_result", label: "What a finished run leaves you holding" },
  { key: "success_example", label: "One run that would count as having worked" },
  { key: "empty_input_behavior", label: "What a run does when there is nothing to work on" },
  { key: "failure_behavior", label: "What a run does when it cannot finish" },
];

const NOT_STATED = "Not stated yet";

export interface AutomationRequestProps {
  /** Hands the typed task over as one turn. Called only with text. */
  onSubmit: (request: string) => void;
  /** A turn is already in flight, so the box is held rather than sending a second one. */
  busy: boolean;
  error: string | null;
}

/** The first screen: one box, asking for the task and nothing else. */
export function AutomationRequest({
  onSubmit,
  busy,
  error,
}: AutomationRequestProps): ReactElement {
  const [text, setText] = useState("");
  const boxId = useId();
  const request = text.trim();
  return (
    <section className="mx-auto flex w-full max-w-2xl flex-col gap-3 p-6">
      <h2 className="font-semibold text-label-1 text-subtitle">What should this automation do?</h2>
      <p className="text-caption text-label-2 leading-snug">
        Say it the way you would say it to a colleague. You can fill in the rest afterwards, and
        anything assumed on your behalf is shown to you before anything runs.
      </p>
      <label htmlFor={boxId} className="sr-only">
        What should this automation do?
      </label>
      <Textarea
        id={boxId}
        rows={4}
        value={text}
        disabled={busy}
        placeholder="Every Monday, tell me which of my open questions the week's reading answered"
        onChange={(event) => setText(event.target.value)}
      />
      <div className="flex items-center gap-3">
        <Button
          onClick={() => onSubmit(request)}
          disabled={busy || request === ""}
          className="h-8 px-4 text-caption"
        >
          {busy ? "Designing" : "Design it"}
        </Button>
        <p className="text-caption text-label-3 leading-snug">
          This designs it and stops. Nothing runs and nothing is scheduled until you say so.
        </p>
      </div>
      {error ? (
        <p role="alert" className="text-caption text-danger-ink">
          {error}
        </p>
      ) : null}
    </section>
  );
}

export interface DesignExchangeProps {
  /** What has been said in this session, as the backend holds it, so a reload still shows it. */
  turns: readonly Turn[];
  /** Hands the next message over as one more turn of the same conversation. Called only with text. */
  onSend: (message: string) => void;
  busy: boolean;
  error: string | null;
  /** Where the full chat for this session lives, for anything this screen does not carry. */
  chatHref: string;
}

/** What was said so far, and the box for saying what should change. */
export function DesignExchange({
  turns,
  onSend,
  busy,
  error,
  chatHref,
}: DesignExchangeProps): ReactElement {
  const [text, setText] = useState("");
  const boxId = useId();
  const message = text.trim();
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-line bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold text-label-1 text-subtitle">What has been said</h2>
        <Link href={chatHref} className="text-caption text-label-2 underline">
          Open this session in the chat
        </Link>
      </div>
      {turns.length === 0 ? (
        <p className="text-caption text-label-3 leading-snug">Nothing yet.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {turns.map((turn, index) => (
            // The position is the identity: two turns can carry the same words.
            <li key={`${turn.who}:${index}`} className="flex flex-col gap-0.5">
              <span className="font-medium text-[11px] text-label-3 uppercase">
                {turn.who === "you" ? "You" : "The agent"}
              </span>
              <p className="whitespace-pre-wrap text-body text-label-1 leading-snug">{turn.text}</p>
            </li>
          ))}
        </ul>
      )}
      <label htmlFor={boxId} className="font-medium text-caption text-label-2">
        What should change?
      </label>
      <Textarea
        id={boxId}
        rows={3}
        value={text}
        disabled={busy}
        placeholder="Read it back to me with the second step split in two"
        onChange={(event) => setText(event.target.value)}
      />
      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={() => {
            onSend(message);
            setText("");
          }}
          disabled={busy || message === ""}
          className="h-8 px-4 text-caption"
        >
          {busy ? "Designing" : "Send"}
        </Button>
        <p className="text-caption text-label-3 leading-snug">
          This changes the design and stops. Nothing runs and nothing is scheduled until you say so.
        </p>
      </div>
      {error ? (
        <p role="alert" className="text-caption text-danger-ink">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/** Whether two statements differ, field by field, so nothing rests on how either was built. */
function differs(edited: WorkflowIntent, recorded: WorkflowIntent): boolean {
  return (
    edited.request !== recorded.request ||
    EXPECTED.some((field) => edited[field.key] !== recorded[field.key]) ||
    edited.trigger !== recorded.trigger ||
    edited.assumptions.length !== recorded.assumptions.length ||
    edited.assumptions.some((assumption, index) => assumption !== recorded.assumptions[index])
  );
}

export interface AutomationIntentProps {
  /** What the draft records, or `null` when nothing has recorded a request for it. */
  intent: WorkflowIntent | null | undefined;
  /** Which expected answers the backend counted as still blank. Never decided here. */
  issues: readonly IntentIssue[];
  /** Records the whole statement, as edited. The panel never patches one field. */
  onRecord: (intent: WorkflowIntent) => void;
  saving: boolean;
  error: string | null;
}

/** The panel beside a drafted automation: what was asked for, what is still open, what was assumed. */
export function AutomationIntent({
  intent,
  issues,
  onRecord,
  saving,
  error,
}: AutomationIntentProps): ReactElement | null {
  const [edited, setEdited] = useState<WorkflowIntent | null>(intent ?? null);
  const recorded = intent ?? null;

  // A fresh statement off the wire replaces whatever was being edited: the server's copy is the
  // one that survives a reload, so showing an older edit beside it would show two answers at once.
  useEffect(() => {
    setEdited(recorded);
  }, [recorded]);

  if (edited === null || recorded === null) {
    return (
      <section className="flex flex-col gap-1.5 rounded-lg border border-line bg-surface p-5">
        <h2 className="font-semibold text-label-1 text-subtitle">What you asked for</h2>
        <p className="text-caption text-label-2 leading-snug">
          Nothing records a request for this draft. Ask for a change below and the agent records what
          you asked for with it.
        </p>
      </section>
    );
  }

  const changed = differs(edited, recorded);
  const open = issues.length;

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-line bg-surface p-5">
      <div className="flex flex-col gap-1.5">
        <h2 className="font-semibold text-label-1 text-subtitle">What you asked for</h2>
        <p className="text-body text-label-1 leading-snug">{recorded.request}</p>
        <p className="text-caption text-label-3 leading-snug">
          Your own words, kept as you wrote them. Everything below can be edited.
        </p>
      </div>

      {open > 0 ? (
        <p className="rounded-md border border-warn-ink/30 bg-warn-soft/60 px-2.5 py-1.5 text-caption text-warn-ink leading-snug">
          {open === 1
            ? "One answer is still open. It is fine to leave it, and the draft can still be saved."
            : `${open} answers are still open. It is fine to leave them, and the draft can still be saved.`}
        </p>
      ) : null}

      <div className="flex flex-col gap-3">
        {EXPECTED.map((field) => (
          <ExpectedAnswer
            key={field.key}
            label={field.label}
            value={edited[field.key]}
            open={issues.includes(field.key)}
            onChange={(value) => setEdited({ ...edited, [field.key]: value })}
          />
        ))}
        <Trigger
          value={edited.trigger}
          onChange={(trigger) => setEdited({ ...edited, trigger })}
        />
      </div>

      <Assumptions
        values={edited.assumptions}
        onChange={(assumptions) => setEdited({ ...edited, assumptions })}
      />

      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={() => onRecord(edited)}
          disabled={saving || !changed}
          className="h-8 px-4 text-caption"
        >
          {saving ? "Saving" : "Save what you asked for"}
        </Button>
        <p className="text-caption text-label-3 leading-snug">
          Saving this records the request. It does not save the workflow and it does not run it.
        </p>
      </div>
      {error ? (
        <p role="alert" className="text-caption text-danger-ink">
          {error}
        </p>
      ) : null}
    </section>
  );
}

function ExpectedAnswer({
  label,
  value,
  open,
  onChange,
}: {
  label: string;
  value: string;
  open: boolean;
  onChange: (value: string) => void;
}): ReactElement {
  const boxId = useId();
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={boxId} className="font-medium text-caption text-label-2">
          {label}
        </label>
        {open ? (
          <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-label-3">
            {NOT_STATED}
          </span>
        ) : null}
      </div>
      <Textarea
        id={boxId}
        rows={2}
        value={value}
        placeholder={NOT_STATED}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

function Trigger({
  value,
  onChange,
}: {
  value: WorkflowIntent["trigger"];
  onChange: (value: WorkflowIntent["trigger"]) => void;
}): ReactElement {
  const boxId = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={boxId} className="font-medium text-caption text-label-2">
        Who starts a run
      </label>
      <Select
        id={boxId}
        value={value}
        onChange={(event) => onChange(event.target.value === "scheduled" ? "scheduled" : "manual")}
      >
        <option value="manual">You start each run</option>
        <option value="scheduled">A schedule starts each run</option>
      </Select>
    </div>
  );
}

function Assumptions({
  values,
  onChange,
}: {
  values: readonly string[];
  onChange: (values: string[]) => void;
}): ReactElement {
  return (
    <div className="flex flex-col gap-2">
      <h3 className="font-medium text-caption text-label-2">Assumed on your behalf</h3>
      {values.length === 0 ? (
        <p className="text-caption text-label-3 leading-snug">
          Nothing was assumed. Every answer above is one you gave.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {values.map((assumption, index) => (
            // The position is the identity here: the text is what the person is editing, so keying
            // on it would remount the box they are typing in on every keystroke.
            <li key={index} className="flex items-start gap-2">
              <span className="mt-1.5 shrink-0 rounded border border-line px-1.5 py-0.5 text-[11px] text-label-3">
                Inferred
              </span>
              <Input
                aria-label={`Assumption ${index + 1}`}
                value={assumption}
                onChange={(event) =>
                  onChange(values.map((each, at) => (at === index ? event.target.value : each)))
                }
              />
              <Button
                variant="secondary"
                aria-label={`Drop assumption ${index + 1}`}
                onClick={() => onChange(values.filter((_, at) => at !== index))}
                className="h-8 shrink-0 px-2.5 text-caption"
              >
                Drop
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
