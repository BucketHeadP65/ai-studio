"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/shared/ui/button";
import { EmptyState } from "@/shared/ui/empty-state";
import Link from "next/link";
import { type ReactElement, type ReactNode, useMemo, useState } from "react";

import { runKeys } from "@/api/query-keys";
import { launchWorkflowRun, sessionsApi } from "@/api/sessions";
import type { RunView } from "@/api/sessions-types";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { LibraryEntry, RunEnvelope, RunIntent } from "@/api/studio-types";
import { BeforeRunDialog } from "@/components/workflows/before-run-dialog";
import { useNowTick } from "@/components/workflows/elapsed";
import { type FleetCard, RunCard } from "@/components/workflows/run-card";
import { runName } from "@/lib/run-name";

// The root station: automation first. Four sections, in the order a person needs them: how to
// start one, what is running now, what is waiting on a decision, and what the last runs produced.
// Chat is a station of its own and stays one press away, from the navigation and from the first
// section here.
//
// Every read is bounded and already exists elsewhere in the app: the live pool whole (the backend
// answers only runs in flight), the saved workflows once, and the newest ended runs at a fixed
// size. Nothing here fetches a workflow's graph (a home full of cards would fetch one per card),
// and the shape of a run belongs on the run's own page.
//
// A run is named by itself: the title its launch gave it, then the saved workflow's own card
// title, then the workflow id. A session's title is never one of those.

/** One card and the identity it keeps across a refresh: a live run's session, an ended run's id. */
interface KeyedCard {
  key: string;
  card: FleetCard;
}

const RECENT_LIMIT = 6;
const SAVED_LIMIT = 6;

const PRIMARY_ACTION =
  "inline-flex items-center gap-1.5 rounded-full bg-label-1 px-4 py-1.5 font-semibold text-sm text-surface transition-colors hover:bg-label-1/85 focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2";
const SECONDARY_ACTION =
  "inline-flex items-center gap-1.5 rounded-full border border-line-strong bg-surface px-4 py-1.5 font-semibold text-label-2 text-sm transition-colors hover:bg-well focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2";

// What the live pool says, or that it said nothing at all. A read that failed knows neither how
// many runs are in flight nor how many wait on a person, and saying either would be a claim.
function summarize(waiting: number, running: number, paused: number, unread: boolean): string {
  if (unread) return "Could not read what is running.";
  const clauses: string[] = [];
  if (waiting > 0) clauses.push(`${waiting} waiting on you`);
  if (running > 0) clauses.push(`${running} running`);
  if (paused > 0) clauses.push(`${paused} paused`);
  return clauses.length === 0 ? "Nothing is running right now." : `${clauses.join(", ")}.`;
}

function Alert({ children }: { children: ReactNode }): ReactElement {
  return (
    <p
      role="alert"
      className="rounded-md border border-warn/40 bg-danger-soft px-3 py-2 text-caption text-danger-ink"
    >
      {children}
    </p>
  );
}

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}): ReactElement {
  return (
    <section aria-labelledby={id} data-section={id} className="flex flex-col gap-3">
      <h2 id={id} className="font-semibold text-label-1 text-subtitle">
        {title}
      </h2>
      {children}
    </section>
  );
}

// One saved workflow as a place to start: what it is called, what it is for in its author's own
// words, and the one act available on it. Setting it up is a dialog, so nothing runs until the
// person has answered what the run needs and said so.
function SavedAutomation({
  card,
  onSetUp,
}: {
  card: LibraryEntry;
  onSetUp: () => void;
}): ReactElement {
  const name = card.title || card.id;
  return (
    <li className="flex flex-col gap-2 rounded-lg border border-line bg-surface px-4 py-3 shadow-sm">
      <Link
        href={`/workflows/library/${encodeURIComponent(card.id)}`}
        className="min-w-0 truncate font-semibold text-body text-label-1 hover:underline"
      >
        {name}
      </Link>
      <p className="line-clamp-2 text-caption text-label-3">
        {card.summary || "Nobody has said what this one is for yet."}
      </p>
      <Button
        aria-label={`Set up ${name}`}
        onClick={onSetUp}
        className="h-8 self-start px-4 text-caption"
      >
        Set up
      </Button>
    </li>
  );
}

export default function HomePage(): ReactElement {
  const queryClient = useQueryClient();
  // The workflow a setup is about, kept while the dialog closes so the dialog sees an open to
  // closed change and hands focus back to the control that opened it.
  const [setup, setSetup] = useState<{ id: string; title: string } | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);

  const runsQuery = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });
  const libraryQuery = useQuery({
    queryKey: ["library"],
    queryFn: () => studioApi.library(),
  });
  const recentQuery = useQuery({
    queryKey: runKeys.recentAtMost(RECENT_LIMIT),
    queryFn: () => studioApi.recentRuns(RECENT_LIMIT),
  });

  // One failed read, stated in both sections it leaves unknown: an empty list and an unread one
  // are different answers, and only one of them means nothing is waiting.
  const liveError = runsQuery.isError ? studioErrorMessage(runsQuery.error) : null;
  const runs: RunView[] = runsQuery.data ?? [];
  const waiting = runs.filter((run) => run.state === "waiting");
  const running = runs.filter((run) => run.state === "running");
  const paused = runs.filter((run) => run.state === "paused");
  const saved: LibraryEntry[] = libraryQuery.data ?? [];
  const recent: RunEnvelope[] = recentQuery.data ?? [];

  // Ticking only while something is actually in flight: an ended run's duration stopped changing
  // when it ended.
  const now = useNowTick(1000, running.length + paused.length + waiting.length > 0);

  const titleById = useMemo(() => new Map(saved.map((card) => [card.id, card.title])), [saved]);

  const launch = useMutation({
    mutationFn: ({ intent, sessionTitle }: { intent: RunIntent; sessionTitle: string }) =>
      launchWorkflowRun(intent, sessionTitle),
    onSuccess: () => {
      setSetupOpen(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
    },
  });
  const decide = useMutation({
    mutationFn: ({
      sessionId,
      approve,
      feedback,
    }: {
      sessionId: string;
      approve: boolean;
      feedback?: string;
    }) => sessionsApi.decide(sessionId, approve, feedback),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.live }),
  });
  const resume = useMutation({
    mutationFn: (sessionId: string) => sessionsApi.resumeRun(sessionId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.live }),
  });
  const pin = useMutation({
    mutationFn: ({ runId, pinned }: { runId: string; pinned: boolean }) =>
      studioApi.pinRun(runId, pinned),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKeys.recent }),
  });

  const activeCards: KeyedCard[] = [
    ...running.map(
      (run): KeyedCard => ({
        key: run.session_id,
        card: {
          kind: "running",
          run,
          name: runName(run.run_title, run.workflow_id, (id) => titleById.get(id)),
          graph: undefined,
          now,
        },
      }),
    ),
    ...paused.map(
      (run): KeyedCard => ({
        key: run.session_id,
        card: {
          kind: "paused",
          run,
          name: runName(run.run_title, run.workflow_id, (id) => titleById.get(id)),
          graph: undefined,
          now,
          onResume: () => resume.mutate(run.session_id),
          resuming: resume.isPending && resume.variables === run.session_id,
        },
      }),
    ),
  ];
  const decisionCards: KeyedCard[] = waiting.map(
    (run): KeyedCard => ({
      key: run.session_id,
      card: {
        kind: "waiting",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => titleById.get(id)),
        graph: undefined,
        now,
        onDecide: (approve, feedback) =>
          decide.mutate({ sessionId: run.session_id, approve, feedback }),
        deciding: decide.isPending && decide.variables?.sessionId === run.session_id,
      },
    }),
  );
  const resultCards: KeyedCard[] = recent.map(
    (run): KeyedCard => ({
      key: run.run_id,
      card: {
        kind: "recent",
        run,
        name: runName(run.run_title, run.workflow_id, (id) => titleById.get(id)),
        graph: undefined,
        onTogglePin: () => pin.mutate({ runId: run.run_id, pinned: !run.pinned }),
        pinning: pin.isPending && pin.variables?.runId === run.run_id,
      },
    }),
  );

  return (
    <div className="-m-6 flex flex-col">
      <div className="flex min-h-[70px] flex-wrap items-center gap-x-3 gap-y-1 border-line border-b bg-surface px-4 py-3 sm:px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Home</h1>
          {/* The one live region on this screen, and polite: the counts settle as runs start and
              end, and nothing else here interrupts a reader to say so. */}
          <p aria-live="polite" className="text-caption text-label-3">
            {summarize(waiting.length, running.length, paused.length, runsQuery.isError)}
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-7 px-4 pt-5 pb-7 sm:px-7">
        <Section id="home-start" title="Start automation">
          <p className="max-w-prose text-body text-label-3">
            Describe what you want automated, or set up one of the workflows you have saved.
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <Link href="/workflows/draft" className={PRIMARY_ACTION}>
              Create workflow
            </Link>
            <Link href="/chat" className={SECONDARY_ACTION}>
              Open chat
            </Link>
          </div>
          {libraryQuery.isError ? (
            <Alert>
              Could not read the saved workflows. {studioErrorMessage(libraryQuery.error)}
            </Alert>
          ) : null}
          {saved.length === 0 ? (
            <EmptyState
              title="No saved workflows yet."
              hint="Create one and it lands here, ready to set up and run."
              className="rounded-lg border border-line border-dashed"
            />
          ) : (
            <ul className="grid gap-3 sm:grid-cols-2">
              {saved.slice(0, SAVED_LIMIT).map((card) => (
                <SavedAutomation
                  key={card.id}
                  card={card}
                  onSetUp={() => {
                    launch.reset();
                    setSetup({ id: card.id, title: card.title || card.id });
                    setSetupOpen(true);
                  }}
                />
              ))}
            </ul>
          )}
          {saved.length > SAVED_LIMIT ? (
            <Link
              href="/workflows/library"
              className="self-start text-caption text-label-2 underline"
            >
              See all {saved.length} saved workflows
            </Link>
          ) : null}
        </Section>

        <Section id="home-active" title="Active automations">
          {liveError !== null ? (
            <Alert>Could not read the runs in flight. {liveError}</Alert>
          ) : activeCards.length === 0 ? (
            <EmptyState
              title="Nothing is running."
              hint="Set up a saved workflow above and it shows here while it works."
              className="rounded-lg border border-line border-dashed"
            />
          ) : (
            <ul className="flex flex-col gap-3">
              {activeCards.map((entry) => (
                <RunCard key={entry.key} card={entry.card} />
              ))}
            </ul>
          )}
        </Section>

        <Section id="home-decisions" title="Needs your decision">
          {liveError !== null ? (
            <Alert>Could not read what is waiting on you. {liveError}</Alert>
          ) : decisionCards.length === 0 ? (
            <EmptyState
              title="No decision is waiting."
              hint="When a run reaches a gate it stops here until you answer it."
              className="rounded-lg border border-line border-dashed"
            />
          ) : (
            <ul className="flex flex-col gap-3">
              {decisionCards.map((entry) => (
                <RunCard key={entry.key} card={entry.card} />
              ))}
            </ul>
          )}
        </Section>

        <Section id="home-results" title="Recent results">
          {recentQuery.isError ? (
            <Alert>
              Could not read the recent results. {studioErrorMessage(recentQuery.error)}
            </Alert>
          ) : null}
          {resultCards.length === 0 ? (
            <EmptyState
              title="No results yet."
              hint="A run that ends lands here with what it produced."
              className="rounded-lg border border-line border-dashed"
            />
          ) : (
            <ul className="flex flex-col gap-3">
              {resultCards.map((entry) => (
                <RunCard key={entry.key} card={entry.card} />
              ))}
            </ul>
          )}
          <Link href="/workflows" className="self-start text-caption text-label-2 underline">
            Open the fleet board
          </Link>
        </Section>
      </div>

      {/* Outside the sections on purpose: a click inside a portal still travels the React tree,
          and a card's own click handler must not claim it. */}
      {setup === null ? null : (
        <BeforeRunDialog
          open={setupOpen}
          workflowId={setup.id}
          workflowTitle={setup.title}
          submitting={launch.isPending}
          error={launch.isError ? studioErrorMessage(launch.error) : null}
          onConfirm={(intent) => launch.mutate({ intent, sessionTitle: setup.title })}
          onCancel={() => setSetupOpen(false)}
        />
      )}
    </div>
  );
}
