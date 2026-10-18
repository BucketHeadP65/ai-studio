"use client";

import { useQuery } from "@tanstack/react-query";
import { type ReactElement, useState } from "react";

import type { Finding, FindingDetail } from "@/api/inspection";
import { inspectionApi } from "@/api/inspection";
import { citesOf, plural, runLocator, sentence, spaced, spanStep } from "@/api/inspection-words";
import { inspectionKeys } from "@/api/query-keys";
import { quotedPhrases } from "@/api/tree-text";

import { EvidenceStory } from "./evidence-story";
import { FindingHeader } from "./finding-header";
import { JudgmentTable } from "./judgment-table";
import { RunTimeline } from "./run-timeline";
import { SpanPane } from "./span-pane";
import { StepDrawer } from "./step-drawer";
import { Trace } from "./trace";

// One finding, whole: judged in a table, told as a story of its cited spans behind a timeline
// of the run, verified in the detail pane, read a step deeper in a drawer, and debugged in the
// trace of what the inspector read.

function findingPosition(
  findings: Finding[] | undefined,
  findingId: number,
): { index: number; total: number; prev: number | null; next: number | null } | null {
  if (findings === undefined) return null;
  const index = findings.findIndex((finding) => finding.finding_id === findingId);
  if (index === -1) return null;
  return {
    index: index + 1,
    total: findings.length,
    prev: index > 0 ? findings[index - 1].finding_id : null,
    next: index < findings.length - 1 ? findings[index + 1].finding_id : null,
  };
}

function spansCaption(spans: string[]): string {
  const steps = new Set(spans.map(spanStep));
  const count = plural(spans.length, "span", "spans");
  if (steps.size === 1) {
    const [step] = steps;
    return `${count}, all in step ${step}`;
  }
  return count;
}

export function FindingDetailView({ detail }: { detail: FindingDetail }): ReactElement {
  const { finding, evidence } = detail;
  const locator = runLocator(finding.source);

  const [hovered, setHovered] = useState<number | null>(null);
  const [picked, setPicked] = useState<string>(evidence[0]?.span ?? "");
  const [readingStep, setReadingStep] = useState<number | null>(null);
  // A challenge's own words, written by `challenge` below and read by the composer in the
  // trace section further down the page.
  const [draft, setDraft] = useState("");
  // Counts each challenge, so `Trace` can scroll to and focus its composer even when the
  // composer is still disabled at the moment of the click (its own conversation not loaded
  // yet) -- `Trace` retries once it is enabled, keyed on this same number.
  const [challenges, setChallenges] = useState(0);
  const [timelineOverride, setTimelineOverride] = useState<number | null>(null);

  const findingsQuery = useQuery({
    queryKey: inspectionKeys.findings({ limit: 200 }),
    queryFn: () => inspectionApi.findings({ limit: 200 }),
  });
  const outlineQuery = useQuery({
    queryKey: inspectionKeys.outline(locator),
    queryFn: () => inspectionApi.outline(locator),
  });

  const spans = evidence.map((piece) => piece.span);
  const phrases = finding.blames.flatMap((blame) => quotedPhrases(blame.why));
  const lit = hovered === null ? new Set<string>() : new Set(citesOf(finding.blames[hovered].why, spans));
  const marked = new Map(spans.map((span) => [spanStep(span), "finding" as const]));
  const selectedStep = picked === "" ? null : spanStep(picked);
  const displayStep = timelineOverride ?? selectedStep;
  const activeOutlineStep = outlineQuery.data?.steps.find((step) => step.step === displayStep) ?? null;

  function pick(span: string): void {
    setPicked(span);
    setTimelineOverride(null);
  }

  function challenge(index: number): void {
    const blame = finding.blames[index];
    setDraft(`On blame ${index + 1}, ${sentence(spaced(blame.root_cause))}: "${blame.why}" `);
    setChallenges((count) => count + 1);
  }

  const timelineCaption = outlineQuery.isError ? (
    <p className="mt-1.5 text-caption text-label-3">The run's outline is not available.</p>
  ) : !outlineQuery.data ? (
    <p className="mt-1.5 text-caption text-label-3">Reading the run's outline.</p>
  ) : activeOutlineStep ? (
    <div className="mt-1.5 flex items-baseline justify-between gap-3 text-caption text-label-2">
      <span>
        step {activeOutlineStep.step}, {plural(activeOutlineStep.messages, "message", "messages")}, {activeOutlineStep.summary}
      </span>
      <button
        type="button"
        className="shrink-0 whitespace-nowrap text-accent"
        onClick={() => setReadingStep(activeOutlineStep.step)}
      >
        Read step {activeOutlineStep.step}
      </button>
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-6">
      <FindingHeader finding={finding} position={findingPosition(findingsQuery.data, finding.finding_id)} />

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="font-semibold text-subtitle">What is to blame</h2>
          <span className="text-caption text-label-3">
            {plural(finding.blames.length, "blame", "blames")}, hover a row to light the spans it cites
          </span>
        </div>
        <JudgmentTable blames={finding.blames} spans={spans} onHover={setHovered} onPick={pick} onChallenge={challenge} />
      </section>

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="font-semibold text-subtitle">The evidence</h2>
          <span className="text-caption text-label-3">{spansCaption(spans)}</span>
        </div>
        <div className="grid grid-cols-[480px_minmax(0,1fr)] items-start gap-5">
          <div className="flex flex-col gap-3">
            <RunTimeline
              steps={outlineQuery.data?.steps ?? []}
              marked={marked}
              selected={displayStep}
              onSelect={setTimelineOverride}
              caption={timelineCaption}
            />
            <EvidenceStory evidence={evidence} phrases={phrases} picked={picked} lit={lit} onPick={pick} />
          </div>
          <SpanPane evidence={evidence} phrases={phrases} picked={picked} onPick={pick} onReadStep={setReadingStep} />
        </div>
      </section>

      <Trace finding={finding} draft={draft} onDraft={setDraft} focusRequest={challenges} spans={spans} />

      <StepDrawer runId={locator} step={readingStep} onClose={() => setReadingStep(null)} />
    </div>
  );
}
