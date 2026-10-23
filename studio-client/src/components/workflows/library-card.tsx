"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactElement } from "react";
import { useState } from "react";

import { runKeys } from "@/api/query-keys";
import { launchWorkflowRun } from "@/api/sessions";
import type { RunView } from "@/api/sessions-types";
import { studioErrorMessage } from "@/api/studio";
import type { LibraryEntry, RunIntent, Trust } from "@/api/studio-types";
import { NodeGlyph } from "@/components/marks/node-glyph";
import { ToneIcon } from "@/components/marks/state-pill";
import { BeforeRunDialog } from "@/components/workflows/before-run-dialog";
import { TrustChip } from "@/components/workflows/trust-chip";
import { libraryHref, runHref } from "@/lib/workflow-href";
import { spendText } from "@/lib/cost-text";
import { cn } from "@/shared/lib/cn";
import { formatRelative } from "@/shared/lib/format-relative";
import { Button } from "@/shared/ui/button";

// One card in the library grid: what the workflow is (its name, version, trust, a one-sentence
// summary, its shape in steps and gates), then what it has been doing lately (a status sentence
// off its live runs and its last finished one), and the one act that starts it, Run, which opens
// the setup every run passes through before a session exists for it. An archived
// card swaps that for Restore, since running an archived artifact is not a supported act here (a
// schedule for it is created from the schedules station's own form, which picks its own
// workflow). The whole card opens the workflow; its own controls never also navigate.

export interface LibraryCardProps {
  card: LibraryEntry;
  liveRuns: RunView[];
  archived: boolean;
  onRestore: (workflowId: string) => void;
  restoring: boolean;
}

// `v{version}` when the library carries one; otherwise the version suffix already sitting in the
// id itself (this library's own ids end in `.vN`), so a card never shows a version-shaped id twice
// over.
function versionTag(card: LibraryEntry): string {
  if (card.version !== null) return `v${card.version}`;
  const match = /\.v(\d+)$/.exec(card.id);
  return match ? `v${match[1]}` : card.id;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function FactRow({
  shape,
  spend,
}: {
  shape: LibraryEntry["shape"];
  spend: LibraryEntry["spend"];
}): ReactElement {
  const noGates = shape.person_gates === 0 && shape.agent_gates === 0;
  return (
    <div className="flex flex-wrap items-center gap-4">
      {shape.steps > 0 ? (
        <span className="flex items-center gap-1.5 font-mono text-caption text-label-2">
          <NodeGlyph kind="step" size={14} />
          {plural(shape.steps, "step")}
        </span>
      ) : null}
      {shape.person_gates > 0 ? (
        <span className="flex items-center gap-1.5 font-mono text-caption text-label-2">
          <NodeGlyph kind="person-gate" size={14} />
          {plural(shape.person_gates, "person gate")}
        </span>
      ) : null}
      {shape.agent_gates > 0 ? (
        <span className="flex items-center gap-1.5 font-mono text-caption text-label-2">
          <NodeGlyph kind="agent-gate" size={14} />
          {plural(shape.agent_gates, "agent gate")}
        </span>
      ) : null}
      {noGates ? <span className="font-mono text-caption text-label-3">no gates</span> : null}
      {spend !== null && spend !== undefined && spend.runs > 0 ? (
        <span className="flex items-center gap-1.5 font-mono text-caption text-label-2">
          {spendText(spend.cost_usd, spend.unpriced)} over{" "}
          {spend.runs === 1 ? "1 run" : `${spend.runs} runs`}
        </span>
      ) : null}
    </div>
  );
}

interface FooterStatus {
  icon: ReactElement | null;
  tone: string;
  text: string;
}

// Why Run is disabled, in words: `null` when the workflow fits this registry. A missing spec and
// missing steps are both named -- a person reading the footer never has to hover a button to find
// out why it won't run.
function unfitReason(card: LibraryEntry): string | null {
  if (card.missing === null) return "the spec could not be read";
  const missingCount = card.missing.length;
  if (missingCount === 0) return null;
  return `${plural(missingCount, "step")} ${missingCount === 1 ? "is" : "are"} missing`;
}

// The one status sentence a card's footer says, first match wins: a workflow that cannot run at
// all outranks everything else, then runs waiting on a decision outrank runs merely in flight,
// which outrank what the last finished run did, which outranks never having run at all. An
// archived card has no Run button, so its unfit reason (if any) never displaces its run history.
function footerStatus(card: LibraryEntry, liveRuns: RunView[], archived: boolean): FooterStatus {
  if (!archived) {
    const reason = unfitReason(card);
    if (reason !== null) {
      return {
        icon: <ToneIcon tone="warning" size={14} />,
        tone: "text-warn-ink",
        text: `Cannot run, ${reason}.`,
      };
    }
  }
  const mine = liveRuns.filter((run) => run.workflow_id === card.id);
  const waiting = mine.filter((run) => run.state === "waiting").length;
  if (waiting > 0) {
    return {
      icon: <ToneIcon tone="waiting" size={14} />,
      tone: "text-warn-ink",
      text: `${plural(waiting, "run")} waiting on you.`,
    };
  }
  const inFlight = mine.filter((run) => run.state === "running" || run.state === "paused").length;
  if (inFlight > 0) {
    return {
      icon: <ToneIcon tone="running" size={14} />,
      tone: "text-accent-ink",
      text: `${plural(inFlight, "run")} in flight.`,
    };
  }
  const last = card.runs.last;
  if (last !== null && (last.outcome === "failed" || last.outcome === "stopped")) {
    return {
      icon: <ToneIcon tone="failed" size={14} />,
      tone: "text-warn-ink",
      text: `Last run failed ${formatRelative(last.ended_at)}.`,
    };
  }
  if (last !== null && last.outcome === "completed") {
    return {
      icon: <ToneIcon tone="completed" size={14} />,
      tone: "text-label-2",
      text: `Last run completed ${formatRelative(last.ended_at)}.`,
    };
  }
  return { icon: null, tone: "text-label-3", text: "Never run." };
}

export function LibraryCard({
  card,
  liveRuns,
  archived,
  onRestore,
  restoring,
}: LibraryCardProps): ReactElement {
  const router = useRouter();
  const queryClient = useQueryClient();

  const [setupOpen, setSetupOpen] = useState(false);
  const run = useMutation({
    mutationFn: (intent: RunIntent) => launchWorkflowRun(intent, card.title),
    onSuccess: () => {
      setSetupOpen(false);
      queryClient.invalidateQueries({ queryKey: runKeys.live });
      router.push(runHref(card.id));
    },
  });

  // Updates the exact list this card is rendering from -- the live library or the archived one --
  // so approving from an archived card is seen there too, not only on a live copy of it.
  function onApproved(trust: Trust) {
    const queryKey = archived ? ["library", "archived"] : ["library"];
    queryClient.setQueryData<LibraryEntry[]>(queryKey, (previous) =>
      previous?.map((item) => (item.id === card.id ? { ...item, trust } : item)),
    );
  }

  const reason = unfitReason(card);
  const fits = reason === null;
  const status = footerStatus(card, liveRuns, archived);
  const runDisabledReason = reason === null ? undefined : `Cannot run, ${reason}.`;

  return (
    <>
      {/* biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it; the keyboard path is the title link inside. */}
      <li
        onClick={(event) => {
          if (
            event.target instanceof Element &&
            event.target.closest("button, input, a, textarea")
          ) {
            return;
          }
          router.push(libraryHref(card.id));
        }}
        className="flex cursor-pointer flex-col overflow-hidden rounded-lg border border-line bg-surface shadow-sm transition-colors hover:border-line-strong"
      >
        <div className="flex flex-1 flex-col gap-2 px-4.5 pt-4 pb-3.5">
          <div className="flex items-start justify-between gap-2">
            <Link
              href={libraryHref(card.id)}
              title={card.summary || card.title}
              className="min-w-0 truncate font-semibold text-label-1 text-subtitle hover:underline"
            >
              {card.title}
            </Link>
            <span className="shrink-0 rounded-full border border-line px-2.5 py-1 font-mono text-label-3 text-micro">
              {versionTag(card)}
            </span>
          </div>
          <TrustChip workflowId={card.id} trust={card.trust} onApproved={onApproved} />
          <p title={card.summary || card.title} className="line-clamp-2 text-body text-label-3">
            {card.summary || "No summary yet."}
          </p>
          <FactRow shape={card.shape} spend={card.spend} />
        </div>

        <div className="flex items-center justify-between gap-3 border-ground border-t bg-well px-4.5 py-3">
          <span className={cn("flex min-w-0 items-center gap-1.5 text-caption", status.tone)}>
            {status.icon}
            <span>{status.text}</span>
          </span>
          {archived ? (
            <Button
              variant="secondary"
              onClick={() => onRestore(card.id)}
              disabled={restoring}
              className="h-8 shrink-0 px-4 text-caption"
            >
              {restoring ? "Restoring" : "Restore"}
            </Button>
          ) : (
            <Button
              onClick={() => {
                run.reset();
                setSetupOpen(true);
              }}
              disabled={run.isPending || !fits}
              title={runDisabledReason}
              className="h-8 shrink-0 px-4 text-caption"
            >
              {run.isPending ? "Starting" : "Run"}
            </Button>
          )}
        </div>
      </li>

      {/* Outside the card body on purpose: a click inside a portal still travels the React tree,
          and the whole card opens the workflow on a click nothing else claimed. */}
      <BeforeRunDialog
        open={setupOpen}
        workflowId={card.id}
        workflowTitle={card.title}
        submitting={run.isPending}
        error={run.isError ? studioErrorMessage(run.error) : null}
        onConfirm={(intent) => run.mutate(intent)}
        onCancel={() => setSetupOpen(false)}
      />
    </>
  );
}
