"use client";

import { Button } from "client-core/ui/button";
import { type ReactElement, useEffect, useRef, useState } from "react";

import { studioApi, studioErrorMessage } from "@/api/studio";
import type { ArtifactAction, ArtifactDocument, ArtifactRef } from "@/api/studio-types";

// What a person may do with what a run produced, drawn from a closed set of intents this app
// already implements. An action is data: a kind, an id, a label, and which declared artifacts it
// concerns. It carries no endpoint, no method and no body, and this surface never builds one --
// every intent below resolves its artifacts through the run's own resolve route and then hands the
// resolved documents to a first-party function. An intent whose function is not installed in this
// deployment says so and does nothing; it never falls back to a route of its own invention.

export interface SaveNoteRequest {
  actionId: string;
  destination: "user_selects";
  document: ArtifactDocument;
}

export interface OwnerReviewRequest {
  actionId: string;
  // The references this run fixed, exactly as this press asked the host about them and exactly as
  // the host confirmed them, in the order the action named them. What was asked about is what is
  // carried on, so nothing a host echoed back can quietly become the thing a person reviews.
  refs: ArtifactRef[];
  documents: ArtifactDocument[];
}

export interface ArtifactActionHandlers {
  saveNote?: (request: SaveNoteRequest) => void | Promise<void>;
  recordOwnerReview?: (request: OwnerReviewRequest) => void | Promise<void>;
}

export interface ArtifactActionsProps {
  runId: string;
  actions: ArtifactAction[];
  // The references this run actually fixed. An action naming anything else has nothing to act on.
  artifacts: ArtifactRef[];
  handlers?: ArtifactActionHandlers;
  // Which action's surface the host currently has open, when it keeps one. That action cannot be
  // pressed again while it is open, so nothing swaps the references out from under a form somebody
  // is reading. Everything else stays pressable, and an action whose surface has closed is offered
  // again: opening a form is not the end of anything, and a person who backed out of one, or whose
  // host refused what they sent, presses again and the run's references are resolved afresh.
  openActionId?: string | null;
}

// The intents this deployment carries out, and what each one says once it has. The record is
// exhaustive over the parsed action kinds, so an intent nobody implements cannot be listed here
// and cannot be parsed at the API boundary either.
const INTENT: Record<ArtifactAction["kind"], string> = {
  save_note: "Opened the preview. Choose where to keep it.",
  record_owner_review: "Opened the review form.",
};

const NOT_INSTALLED = "This action is not available here yet.";

// What one press of an action has come to. `opened` is the end of the press, never the end of
// the act: these intents open a surface for a person to work in, and what they do there is theirs.
type ActionState =
  | { phase: "idle" }
  | { phase: "working" }
  | { phase: "opened" }
  | { phase: "refused"; message: string };

const IDLE: ActionState = { phase: "idle" };

// Which declared artifacts one action concerns, in the order its author named them.
function wanted(action: ArtifactAction): string[] {
  return action.kind === "save_note" ? [action.artifact_id] : [...action.artifact_ids];
}

// One press, fully decided before anything happens: which references it resolves, and the
// first-party function it hands the resolved documents to.
type Press =
  | {
      kind: "save_note";
      ref: ArtifactRef;
      destination: "user_selects";
      open: (request: SaveNoteRequest) => void | Promise<void>;
    }
  | {
      kind: "record_owner_review";
      refs: ArtifactRef[];
      open: (request: OwnerReviewRequest) => void | Promise<void>;
    };

// What pressing this action would do, or `null` when it would do nothing: a reference the run never
// fixed, or an intent this deployment has installed no function for. Everything a press needs is
// found here, so the press itself has nothing left to check.
function ready(
  action: ArtifactAction,
  artifacts: ArtifactRef[],
  handlers: ArtifactActionHandlers,
): Press | null {
  if (action.kind === "save_note") {
    const ref = artifacts.find((candidate) => candidate.id === action.artifact_id);
    const open = handlers.saveNote;
    if (ref === undefined || open === undefined) return null;
    return { kind: "save_note", ref, destination: action.destination, open };
  }
  const found = action.artifact_ids.map((id) => artifacts.find((candidate) => candidate.id === id));
  const refs = found.flatMap((ref) => (ref === undefined ? [] : [ref]));
  const open = handlers.recordOwnerReview;
  if (refs.length !== found.length || open === undefined) return null;
  return { kind: "record_owner_review", refs, open };
}

// Why an action cannot be pressed, or `null` when it can.
function unavailable(
  action: ArtifactAction,
  artifacts: ArtifactRef[],
  handlers: ArtifactActionHandlers,
): string | null {
  const missing = wanted(action).filter((id) => !artifacts.some((ref) => ref.id === id));
  if (missing.length > 0) {
    return `This run fixed no value for ${missing.join(", ")}, so there is nothing to act on.`;
  }
  return ready(action, artifacts, handlers) === null ? NOT_INSTALLED : null;
}

export function ArtifactActions({
  runId,
  actions,
  artifacts,
  handlers = {},
  openActionId,
}: ArtifactActionsProps): ReactElement | null {
  const [states, setStates] = useState<Record<string, ActionState>>({});
  const [renderedRun, setRenderedRun] = useState(runId);
  // The run on screen, readable by a press that began before the reader moved to another one.
  const viewedRun = useRef(runId);

  // What a press said belongs to the run it was pressed on. A different run is a different set of
  // values behind identically named actions, so this panel starts over: its own words about the
  // run being left go with it, and nothing of theirs is read under the new run's buttons. Done
  // while rendering the run that arrives, not after, so no frame is ever painted carrying them.
  if (renderedRun !== runId) {
    setRenderedRun(runId);
    setStates({});
    viewedRun.current = runId;
  }

  // A surface the host has closed leaves nothing behind: the action that opened it goes back to
  // reading exactly as it was offered. A refusal is kept, because it is the only place the host's
  // own words about that press are shown, and it disables nothing.
  useEffect(() => {
    setStates((held) => {
      const kept = Object.entries(held).filter(
        ([id, state]) => state.phase !== "opened" || id === openActionId,
      );
      return kept.length === Object.keys(held).length ? held : Object.fromEntries(kept);
    });
  }, [openActionId]);

  if (actions.length === 0) return null;

  function settle(actionId: string, state: ActionState): void {
    setStates((held) => ({ ...held, [actionId]: state }));
  }

  // One press, one pass: every declared reference is resolved through the run's own route, in the
  // order the action named them, and only then is the intent's own function called. A refusal from
  // the host stops the press and is shown in the host's own words.
  //
  // The whole press belongs to the run it began on. A reader who moves to another run while its
  // references are resolving has left it behind: what comes back opens nothing and says nothing,
  // rather than putting one run's values in front of somebody reading another's.
  async function press(action: ArtifactAction): Promise<void> {
    const plan = ready(action, artifacts, handlers);
    if (plan === null) return;
    const pressedOn = runId;
    const stillViewed = (): boolean => viewedRun.current === pressedOn;
    settle(action.id, { phase: "working" });
    try {
      if (plan.kind === "save_note") {
        const document = await studioApi.resolveArtifact(pressedOn, plan.ref);
        if (!stillViewed()) return;
        await plan.open({ actionId: action.id, destination: plan.destination, document });
      } else {
        const documents: ArtifactDocument[] = [];
        for (const ref of plan.refs) documents.push(await studioApi.resolveArtifact(pressedOn, ref));
        if (!stillViewed()) return;
        await plan.open({ actionId: action.id, refs: plan.refs, documents });
      }
    } catch (error) {
      if (!stillViewed()) return;
      settle(action.id, { phase: "refused", message: studioErrorMessage(error) });
      return;
    }
    if (!stillViewed()) return;
    settle(action.id, { phase: "opened" });
  }

  return (
    <div className="flex flex-col gap-2">
      {actions.map((action) => {
        const blocked = unavailable(action, artifacts, handlers);
        const state = states[action.id] ?? IDLE;
        return (
          <div key={action.id} className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              onClick={() => void press(action)}
              disabled={
                blocked !== null || state.phase === "working" || action.id === openActionId
              }
              className="h-8 px-3 text-caption"
            >
              {state.phase === "working" ? "Opening" : action.label}
            </Button>
            {blocked !== null ? <span className="text-caption text-label-3">{blocked}</span> : null}
            {state.phase === "opened" ? (
              // Announced like the refusal beside it. A press that worked and a press that was
              // refused are equally worth hearing, and a surface that says only one of the two
              // leaves a reader who cannot see the screen waiting on nothing.
              <span role="status" className="text-caption text-label-2">
                {INTENT[action.kind]}
              </span>
            ) : null}
            {state.phase === "refused" ? (
              <span role="alert" className="text-caption text-danger-ink">
                {state.message}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
