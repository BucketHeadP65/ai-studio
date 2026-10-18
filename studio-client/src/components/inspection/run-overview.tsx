"use client";

import Link from "next/link";
import { type ReactElement, type ReactNode, useState } from "react";

import type { RunOutline, RunOverview } from "@/api/inspection";
import { duration, longDate, plural, runName, shortDate, spaced } from "@/api/inspection-words";

import {
  findingOfQuestion,
  jobLabel,
  jobsPhrase,
  leadsPhrase,
  markedSteps,
  questionsPhrase,
  type Settled,
  settledOf,
} from "./run-overview-facts";
import { RunTimeline } from "./run-timeline";
import { StepDrawer } from "./step-drawer";
import { SurfaceChip } from "./surface-chip";

// One run, whole: the timeline the inspection walked, then everything it left behind, table by table:
// the leads the rules raised, the findings that came of them, the questions asked to settle one, and
// the jobs the pipeline ran to do all of it.

const CELL = "px-3 py-2 text-left align-top";

function metaPieces(outline: RunOutline): ReactNode[] {
  const pieces: ReactNode[] = [];
  if (outline.started_at !== null) pieces.push(`started ${longDate(outline.started_at)}`);
  if (outline.started_at !== null && outline.ended_at !== null) {
    const span = duration(outline.started_at, outline.ended_at);
    if (span !== "") pieces.push(span);
  }
  pieces.push(plural(outline.steps.length, "step", "steps"));
  pieces.push(plural(outline.tool_calls, "tool call", "tool calls"));
  if (outline.model !== null) {
    pieces.push(
      <>
        on <span className="font-mono">{outline.model}</span>
      </>,
    );
  }
  if (outline.ended !== null) pieces.push(spaced(outline.ended));
  return pieces;
}

function RunMeta({ outline }: { outline: RunOutline | null }): ReactElement {
  if (outline === null) {
    return <p className="mt-2 text-caption text-label-2">The run itself can no longer be read.</p>;
  }
  return (
    <p className="mt-2 text-caption text-label-2">
      {metaPieces(outline).map((piece, index) => (
        <span key={index}>
          {index > 0 ? ", " : ""}
          {piece}
        </span>
      ))}
    </p>
  );
}

const LEGEND: { swatch: string; words: string }[] = [
  { swatch: "bg-accent", words: "step with a finding" },
  { swatch: "bg-accent/40", words: "step with a lead the inspector dismissed" },
  { swatch: "bg-danger-ink/50", words: "step with an unresolved lead" },
  { swatch: "bg-line", words: "no recorded finding, width is the step's message count" },
];

function TimelineLegend(): ReactElement {
  return (
    <div className="mt-2 flex flex-wrap gap-4">
      {LEGEND.map((item) => (
        <span key={item.words} className="inline-flex items-center gap-1.5 text-caption text-label-2">
          <span className={`size-2.5 rounded-sm ${item.swatch}`} />
          {item.words}
        </span>
      ))}
    </div>
  );
}

function SettledCell({ settled }: { settled: Settled }): ReactElement {
  if (settled.kind === "finding") {
    return (
      <>
        Yes, finding <Link href={`/inspection/${settled.finding.finding_id}`}>{spaced(settled.finding.symptom)}</Link>
      </>
    );
  }
  if (settled.kind === "dismissed") {
    if (settled.refusal === "") return <>Yes, dismissed by the inspector, no reason recorded.</>;
    return (
      <>
        Yes, dismissed by the inspector: <span className="text-danger-ink">{settled.refusal}</span>
      </>
    );
  }
  if (settled.kind === "undecided" || settled.kind === "invalid") {
    const label = settled.kind === "undecided" ? "Undecided" : "Invalid inspector answer";
    return <>{label}, still unresolved{settled.refusal === "" ? "." : `: ${settled.refusal}`}</>;
  }
  return <>Not yet</>;
}

export function RunOverviewView({
  runId,
  overview,
  outline,
}: {
  runId: string;
  overview: RunOverview;
  outline: RunOutline | null;
}): ReactElement {
  const [readingStep, setReadingStep] = useState<number | null>(null);

  const { agent, short } = runName(runId);
  const title = outline?.agent ?? agent;

  return (
    <div className="flex flex-col gap-6">
      <nav aria-label="Breadcrumb" className="flex h-8 items-center gap-2 text-caption">
        <Link href="/inspection">Inspection</Link>
        <span className="text-label-3">/</span>
        <span className="text-label-2">Runs</span>
        <span className="text-label-3">/</span>
        <span className="font-mono text-label-2">{runId}</span>
      </nav>

      <div className="flex justify-between border-line border-b pb-4">
        <div>
          <div className="flex items-baseline gap-3">
            <h1 className="text-title font-semibold">{title}</h1>
            <span className="font-mono text-subtitle text-label-2">{short}</span>
          </div>
          <RunMeta outline={outline} />
          <p className="text-caption text-label-3">Click a step on the timeline to read it.</p>
        </div>
      </div>

      <RunTimeline
        steps={outline?.steps ?? []}
        marked={markedSteps(overview)}
        selected={null}
        onSelect={setReadingStep}
        caption={<TimelineLegend />}
      />

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-subtitle font-semibold">Leads the rules left</h2>
          <span className="text-caption text-label-3">{leadsPhrase(overview.leads, overview.findings)}</span>
        </div>
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-body">
            <caption className="sr-only">Leads the rules left</caption>
            <thead className="border-line border-b bg-well text-caption text-label-3">
              <tr>
                <th className={CELL}>Symptom</th>
                <th className={CELL}>Spans</th>
                <th className={CELL}>Rule</th>
                <th className={CELL}>Settled</th>
              </tr>
            </thead>
            <tbody>
              {overview.leads.map((lead) => {
                const settled = settledOf(lead, overview.findings);
                return (
                  <tr key={lead.lead_id} className="border-line border-b last:border-b-0">
                    <td className={`${CELL} font-medium text-label-1`}>{spaced(lead.symptom)}</td>
                    <td className={`${CELL} font-mono`}>{lead.spans.join(" ")}</td>
                    <td className={`${CELL} font-mono text-label-3`}>{lead.found_by}</td>
                    <td className={CELL}>
                      <SettledCell settled={settled} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-subtitle font-semibold">Findings</h2>
          <span className="text-caption text-label-3">{overview.findings.length}</span>
        </div>
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-body">
            <caption className="sr-only">Findings</caption>
            <thead className="border-line border-b bg-well text-caption text-label-3">
              <tr>
                <th className={CELL}>Symptom</th>
                <th className={CELL}>Found by</th>
                <th className={CELL}>Blame surfaces</th>
                <th className={CELL}>Decided</th>
                <th className={CELL} />
              </tr>
            </thead>
            <tbody>
              {overview.findings.map((finding) => (
                <tr key={finding.finding_id} className="border-line border-b last:border-b-0">
                  <td className={`${CELL} font-medium text-label-1`}>{spaced(finding.symptom)}</td>
                  <td className={CELL}>
                    {finding.found_by === "inspector" ? (
                      finding.model === null ? (
                        <>
                          inspector on <span className="text-label-3">none</span>
                        </>
                      ) : (
                        <>
                          inspector on <span className="font-mono">{finding.model}</span>
                        </>
                      )
                    ) : finding.lead_found_by === null ? (
                      <>
                        rule <span className="text-label-3">none</span>
                      </>
                    ) : (
                      <>
                        rule <span className="font-mono">{finding.lead_found_by}</span>
                      </>
                    )}
                  </td>
                  <td className={CELL}>
                    <div className="flex flex-wrap gap-1">
                      {finding.blames.map((blame, index) => (
                        <SurfaceChip key={index} blame={blame} />
                      ))}
                    </div>
                  </td>
                  <td className={`${CELL} text-caption text-label-3`}>{shortDate(finding.decided_at)}</td>
                  <td className={CELL}>
                    <Link href={`/inspection/${finding.finding_id}`}>Open</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-subtitle font-semibold">Questions asked</h2>
          <span className="text-caption text-label-3">{questionsPhrase(overview.questions)}</span>
        </div>
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-body">
            <caption className="sr-only">Questions asked</caption>
            <thead className="border-line border-b bg-well text-caption text-label-3">
              <tr>
                <th className={CELL}>Kind</th>
                <th className={CELL}>Tier</th>
                <th className={CELL}>Model</th>
                <th className={CELL}>Answer</th>
                <th className={CELL}>Asked</th>
                <th className={CELL} />
              </tr>
            </thead>
            <tbody>
              {overview.questions.map((question) => {
                const decided = findingOfQuestion(question, overview.findings);
                return (
                  <tr key={question.question_id} className="border-line border-b last:border-b-0">
                    <td className={`${CELL} font-medium text-label-1`}>{question.kind}</td>
                    <td className={`${CELL} text-label-3`}>{question.tier}</td>
                    <td className={`${CELL} font-mono`}>{question.model}</td>
                    <td className={CELL}>{question.answer ?? "no answer yet"}</td>
                    <td className={`${CELL} text-caption text-label-3`}>{shortDate(question.asked_at)}</td>
                    <td className={CELL}>
                      {decided === null ? "none" : <Link href={`/inspection/${decided.finding_id}#trace`}>Reading log</Link>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="flex flex-col gap-2">
        <div className="flex items-baseline gap-2">
          <h2 className="text-subtitle font-semibold">Jobs the pipeline issued</h2>
          <span className="text-caption text-label-3">{jobsPhrase(overview.jobs)}</span>
        </div>
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-body">
            <caption className="sr-only">Jobs the pipeline issued</caption>
            <thead className="border-line border-b bg-well text-caption text-label-3">
              <tr>
                <th className={CELL}>Job</th>
                <th className={CELL}>State</th>
                <th className={CELL}>Attempts</th>
                <th className={CELL}>Result</th>
                <th className={CELL}>Issued</th>
              </tr>
            </thead>
            <tbody>
              {overview.jobs.map((job) => {
                const label = jobLabel(job, overview.leads);
                return (
                  <tr key={job.id} className="border-line border-b last:border-b-0">
                    <td className={CELL}>
                      <span className="font-mono">{label.kind}</span>
                      {label.detail === "" ? null : <span className="ml-2 text-label-3">{label.detail}</span>}
                    </td>
                    <td className={CELL}>{job.state}</td>
                    <td className={CELL}>{job.attempts}</td>
                    <td className={CELL}>
                      {job.result !== null ? job.result : job.last_error !== null ? <span className="text-danger-ink">{job.last_error}</span> : "none"}
                    </td>
                    <td className={`${CELL} text-caption text-label-3`}>{shortDate(job.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <StepDrawer runId={runId} step={readingStep} onClose={() => setReadingStep(null)} />
    </div>
  );
}
