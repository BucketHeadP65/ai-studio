"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Modal, ModalDescription, ModalTitle } from "@/shared/ui/modal";
import { Textarea } from "@/shared/ui/textarea";
import { type ReactElement, useId, useState } from "react";

import { studioApi, studioErrorMessage } from "@/api/studio";
import type { ArtifactRef, OwnerReview, OwnerReviewResult } from "@/api/studio-types";
import { RadioCardGroup } from "@/components/workflows/radio-card";

// Writing down what a person made of what a run produced.
//
// It records a reading of exactly the values named here, by their own references and digests:
// the host re-checks every one against the run's own record before it writes anything, so a
// value that has moved on is a refusal the person can act on, never a review of something they
// did not read. Nothing here changes what the run did, whether it succeeded, or whether it
// passed its gates. A review is an event beside those facts.
//
// Whoever decided types their own name. This deployment has no accounts and no logins, so the
// name is text, recorded as given and never filled in on anybody's behalf.
//
// Mounted only while a review is being written. Each opening is its own mount, which is what
// gives it its own `review_id`: every attempt within one opening carries that id, so a retry
// after a lost answer meets the event it already wrote instead of writing a second one. Changing
// the words and sending them under that same id is refused by the host, and the refusal is shown
// as it arrived.

export interface OwnerReviewFormProps {
  runId: string;
  /** The references the host minted and has just re-checked, in the order the action named them. */
  refs: ArtifactRef[];
  onClose: () => void;
}

type Verdict = OwnerReview["outcome"] | "";

const VERDICTS = [
  { value: "accepted", label: "Accepted", description: "This is what I needed." },
  { value: "rejected", label: "Rejected", description: "This is not what I needed." },
] as const;

// What the person is told once their decision has landed. A repeat of a submission already on
// record adds nothing, and says so rather than reading as a second review.
function settledReading(result: OwnerReviewResult): string {
  return result.created
    ? "Recorded. It sits beside this run, as your reading of these values."
    : "This exact review was already recorded, so nothing was added.";
}

export function OwnerReviewForm({ runId, refs, onClose }: OwnerReviewFormProps): ReactElement {
  const nameId = useId();
  const reasonId = useId();
  const [reviewId] = useState(() => crypto.randomUUID());
  const [actor, setActor] = useState("");
  const [verdict, setVerdict] = useState<Verdict>("");
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const record = useMutation({
    retry: 0,
    mutationFn: (outcome: OwnerReview["outcome"]): Promise<OwnerReviewResult> =>
      studioApi.recordOwnerReview(runId, {
        review_id: reviewId,
        actor: actor.trim(),
        outcome,
        reason: reason.trim(),
        artifact_refs: refs,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["studio", "run-record", runId] });
    },
  });

  const settled = record.data ?? null;
  const ready = actor.trim() !== "" && reason.trim() !== "" && verdict !== "";

  return (
    // Esc and a press on the backdrop ask to close, and the answer is no while a submission is in
    // flight: that answer is the only thing that tells the person whether their reading landed,
    // and under which id, so it is not thrown away by a stray key. Cancel is disabled then for
    // the same reason.
    <Modal
      open
      onClose={() => {
        if (!record.isPending) onClose();
      }}
      className="max-w-lg"
    >
      <div className="flex max-h-[80vh] flex-col gap-3 overflow-y-auto">
        <ModalTitle className="text-subtitle">Record your review</ModalTitle>
        <ModalDescription className="mt-0 text-caption text-label-3">
          Your reading of these exact values. It changes nothing about what the run did.
        </ModalDescription>

        <div className="flex flex-col gap-1.5 rounded-md border border-line bg-well px-3 py-2.5">
          <p className="font-semibold text-label-3 text-micro uppercase tracking-caps">
            What you are reviewing
          </p>
          <ul className="flex flex-col gap-1">
            {refs.map((ref) => (
              <li key={ref.id} className="flex flex-wrap items-baseline gap-2">
                <span className="text-caption text-label-1">{ref.label}</span>
                <span className="font-mono text-label-3 text-micro">{ref.media_type}</span>
                <span className="font-mono text-label-3 text-micro">
                  {ref.value_digest.slice(0, 12)}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div className="flex flex-col gap-1">
          <label
            htmlFor={nameId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Your name
          </label>
          <Input
            id={nameId}
            value={actor}
            disabled={record.isPending || settled !== null}
            onChange={(event) => setActor(event.target.value)}
          />
          <p className="text-caption text-label-3">
            Recorded as who decided, in your own words. There are no accounts here, so nothing
            fills this in for you.
          </p>
        </div>

        <RadioCardGroup<Verdict>
          name="owner-review-verdict"
          legend="Your verdict"
          value={verdict}
          onChange={setVerdict}
          options={VERDICTS}
          disabled={record.isPending || settled !== null}
        />

        <div className="flex flex-col gap-1">
          <label
            htmlFor={reasonId}
            className="font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            Why
          </label>
          <Textarea
            id={reasonId}
            value={reason}
            rows={3}
            disabled={record.isPending || settled !== null}
            onChange={(event) => setReason(event.target.value)}
          />
        </div>

        {record.isError ? (
          <p role="alert" className="text-caption text-danger-ink">
            {studioErrorMessage(record.error)}
          </p>
        ) : null}
        {settled !== null ? (
          <p role="status" className="text-caption text-label-2">
            {settledReading(settled)}
          </p>
        ) : null}

        <div className="mt-1 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose} disabled={record.isPending}>
            {settled !== null ? "Close" : "Cancel"}
          </Button>
          {settled === null ? (
            <Button
              onClick={() => {
                if (verdict !== "") record.mutate(verdict);
              }}
              disabled={record.isPending || !ready}
            >
              {record.isPending ? "Recording" : "Record"}
            </Button>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
