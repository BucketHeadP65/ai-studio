"use client";

import type { ReactElement } from "react";

import { formatRelative } from "@/shared/lib/format-relative";

import type { OwnerReview, WorkflowDetail } from "@/api/studio-types";
import {
  type ProvenanceActor,
  ProvenanceChip,
  provenance,
} from "@/components/marks/provenance-chip";
import { StatePill, type PillTone } from "@/components/marks/state-pill";
import { TRUST_LABEL } from "@/components/workflows/trust-chip";

// The workflow's own earned trust, read plainly beside the run it is showing rather than left
// off the page entirely: its level, how many runs stand behind that level, and how many of those
// failed. Structural grading only adds its line once something has actually graded the artifact
// -- an ungraded workflow states nothing about a mean it does not have.
//
// `gateDecisions`, when the run being shown carries one, names who decided each of its own
// gates -- a person, this run's own orchestrator, or nobody asked -- the same three buckets
// OutcomeStrip already counts, now naming which gate landed in which. Every row's sentence comes
// from the shared `provenance` builders, so the words match the gate trail everywhere else on
// the page.
//
// `ownerReviews` is the separate question the execution facts above can never answer: what a
// person made of what the run produced. It gets its own block, so nothing a run did on its own
// reads as anybody's approval. Undefined is no run in view. An empty list is a run in view
// nobody has reviewed, which is unknown acceptance and says exactly that. Every review is shown,
// oldest first: a later one is another event, never a correction of the one before it.

export interface RunEvidencePanelProps {
  trust: WorkflowDetail["trust"];
  gateDecisions?: {
    approved: string[];
    orchestrated: string[];
    auto: string[];
  };
  ownerReviews?: OwnerReview[];
}

const VERDICT_TONE: Record<OwnerReview["outcome"], PillTone> = {
  accepted: "completed",
  rejected: "failed",
};

const VERDICT_WORD: Record<OwnerReview["outcome"], string> = {
  accepted: "Accepted",
  rejected: "Rejected",
};

function OwnerReviewList({ reviews }: { reviews: OwnerReview[] }): ReactElement {
  if (reviews.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-surface px-3 py-2.5 shadow-sm">
        <p className="text-body font-medium text-label-1">Not reviewed</p>
        <p className="mt-1 text-caption text-label-2">
          Nobody has recorded a reading of what this run produced, so whether it was any use is
          unknown.
        </p>
      </div>
    );
  }
  return (
    <ul className="space-y-1.5">
      {reviews.map((review) => (
        <li
          key={review.review_id}
          className="rounded-lg border border-line bg-surface px-3 py-2.5 shadow-sm"
        >
          <div className="flex flex-wrap items-center gap-2">
            <StatePill tone={VERDICT_TONE[review.outcome]} size="sm">
              {VERDICT_WORD[review.outcome]}
            </StatePill>
            <span className="text-caption text-label-1">{review.actor}</span>
            <time
              dateTime={review.at}
              title={new Date(review.at).toLocaleString()}
              className="text-caption text-label-3"
            >
              {formatRelative(review.at)}
            </time>
          </div>
          <p className="mt-1 text-caption text-label-2">{review.reason}</p>
          <ul className="mt-1 space-y-0.5">
            {review.artifact_refs.map((ref) => (
              <li key={ref.id} className="flex flex-wrap items-baseline gap-1.5">
                <span className="text-label-3 text-micro">{ref.label}</span>
                <span className="font-mono text-label-3 text-micro">
                  {ref.value_digest.slice(0, 12)}
                </span>
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

interface GateDecisionRow {
  id: string;
  actor: ProvenanceActor;
  sentence: string;
}

function GateDecisionList({
  gateDecisions,
}: {
  gateDecisions: NonNullable<RunEvidencePanelProps["gateDecisions"]>;
}): ReactElement | null {
  const rows: GateDecisionRow[] = [
    ...gateDecisions.approved.map((id) => ({
      id,
      actor: "person-approved" as const,
      sentence: provenance.approvedByPerson(),
    })),
    ...gateDecisions.orchestrated.map((id) => ({
      id,
      actor: "agent-decided" as const,
      sentence: provenance.decidedByAgent([id]),
    })),
    ...gateDecisions.auto.map((id) => ({
      id,
      actor: "automatic" as const,
      sentence: provenance.passedAutomatically([id]),
    })),
  ];
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <p className="text-caption text-label-2">Who decided each gate</p>
      <ul className="space-y-1.5">
        {rows.map((row) => (
          <li key={row.id} className="rounded-md border border-line bg-well/60 px-2.5 py-2">
            <p className="font-mono text-caption text-label-1">{row.id}</p>
            <div className="mt-1">
              <ProvenanceChip actor={row.actor} variant="inline">
                {row.sentence}
              </ProvenanceChip>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RunEvidencePanel({
  trust,
  gateDecisions,
  ownerReviews,
}: RunEvidencePanelProps): ReactElement {
  return (
    <>
      <section data-section="execution-evidence" className="space-y-1.5">
        <h3 className="font-semibold text-label-3 text-micro uppercase tracking-caps">Evidence</h3>
        <div className="rounded-lg border border-line bg-surface px-3 py-2.5 shadow-sm">
          <p className="text-body font-medium text-label-1">
            {TRUST_LABEL[trust.level] ?? trust.level}
          </p>
          <p className="mt-1 text-caption text-label-2">
            {trust.runs_with_evidence} completed runs, {trust.failed_runs} failed runs.
            {trust.structural_grades > 0 && trust.structural_mean !== null
              ? ` Structural mean ${trust.structural_mean.toFixed(2)} over ${trust.structural_grades} grades.`
              : ""}
          </p>
        </div>
        {gateDecisions ? <GateDecisionList gateDecisions={gateDecisions} /> : null}
      </section>
      {ownerReviews !== undefined ? (
        <section data-section="owner-review" className="space-y-1.5">
          <h3 className="font-semibold text-label-3 text-micro uppercase tracking-caps">
            Owner review
          </h3>
          <OwnerReviewList reviews={ownerReviews} />
        </section>
      ) : null}
    </>
  );
}
