import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ArtifactRef } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { OwnerReviewForm } from "./owner-review-form";

// What a person records about what a run produced, and everything the surface refuses to decide
// for them: who they are, whether they accept it, and why. The references are the ones the host
// minted, sent back exactly as they arrived, so the host can refuse a reading of a value that has
// moved on instead of quietly accepting a newer one in its place.

const BASE = "http://studio.test";
const REVIEW_ROUTE = `${BASE}/api/runs/history/run-1/review`;

const DRAFT: ArtifactRef = {
  id: "draft",
  label: "The draft",
  run_id: "run-1",
  node_id: "draft_report",
  turn: null,
  value_digest: "a".repeat(64),
  media_type: "text/markdown",
};

interface Submission {
  review_id: string;
  actor: string;
  outcome: string;
  reason: string;
  artifact_refs: ArtifactRef[];
}

function reviewed(review: Partial<Submission>, at = "2026-09-16T09:10:00Z") {
  return {
    review_id: review.review_id ?? "r1",
    actor: review.actor ?? "Alex",
    outcome: review.outcome ?? "accepted",
    reason: review.reason ?? "It answered the question.",
    artifact_refs: review.artifact_refs ?? [DRAFT],
    source: "app:harness",
    at,
  };
}

// The host as it behaves when nothing is wrong: it writes the first submission under a review id
// and meets every identical one after it with the event it already wrote.
function recording(sent: Submission[]): void {
  server.use(
    http.post(REVIEW_ROUTE, async ({ request }) => {
      const body = (await request.json()) as Submission;
      const repeat = sent.some((earlier) => earlier.review_id === body.review_id);
      sent.push(body);
      return HttpResponse.json({ review: reviewed(body), created: !repeat });
    }),
  );
}

async function fillIn(name: string, verdict: string, why: string): Promise<void> {
  await userEvent.type(screen.getByLabelText("Your name"), name);
  await userEvent.click(screen.getByRole("radio", { name: verdict }));
  await userEvent.type(screen.getByLabelText("Why"), why);
}

describe("OwnerReviewForm", () => {
  it("names the exact values being reviewed, by label and digest", () => {
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    expect(screen.getByText("What you are reviewing")).toBeInTheDocument();
    expect(screen.getByText("The draft")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
  });

  it("opens with no name filled in and records nothing until a name, a verdict and a reason are there", async () => {
    const sent: Submission[] = [];
    recording(sent);
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    expect(screen.getByLabelText("Your name")).toHaveValue("");
    expect(screen.getByRole("radio", { name: "Accepted" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Rejected" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Your name"), "Alex");
    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "Accepted" }));
    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Why"), "It answered the question.");
    expect(screen.getByRole("button", { name: "Record" })).toBeEnabled();
    expect(sent).toHaveLength(0);
  });

  it("sends the typed name, the verdict, the reason and the host's own references", async () => {
    const sent: Submission[] = [];
    recording(sent);
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      actor: "Alex",
      outcome: "accepted",
      reason: "It answered the question.",
      artifact_refs: [DRAFT],
    });
    expect(sent[0]?.review_id).not.toBe("");
    expect(await screen.findByRole("status")).toHaveTextContent("Recorded.");
  });

  it("records a rejection with its reason, the same way it records an acceptance", async () => {
    const sent: Submission[] = [];
    recording(sent);
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Rejected", "It never said the second section was empty.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      outcome: "rejected",
      reason: "It never said the second section was empty.",
    });
  });

  it("retries under the same review id after a lost answer, and the repeat adds nothing", async () => {
    const sent: Submission[] = [];
    let first = true;
    server.use(
      http.post(REVIEW_ROUTE, async ({ request }) => {
        const body = (await request.json()) as Submission;
        sent.push(body);
        if (first) {
          first = false;
          return HttpResponse.error();
        }
        return HttpResponse.json({ review: reviewed(body), created: false });
      }),
    );
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]?.review_id).toBe(sent[0]?.review_id);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "This exact review was already recorded, so nothing was added.",
    );
  });

  it("keeps the words and the references when the host refuses a value that has moved on", async () => {
    const sent: Submission[] = [];
    server.use(
      http.post(REVIEW_ROUTE, async ({ request }) => {
        sent.push((await request.json()) as Submission);
        return HttpResponse.json(
          { ok: false, message: "this reference no longer names the value that run fixed" },
          { status: 409 },
        );
      }),
    );
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this reference no longer names the value that run fixed",
    );
    expect(screen.getByLabelText("Why")).toHaveValue("It answered the question.");
    expect(screen.getByRole("button", { name: "Record" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(sent[0]?.artifact_refs).toEqual([DRAFT]);
  });

  it("says a value it cannot read is unreadable, and stays open for another try", async () => {
    server.use(
      http.post(REVIEW_ROUTE, () =>
        HttpResponse.json(
          { ok: false, message: "this run fixed no value under that artifact's source" },
          { status: 404 },
        ),
      ),
    );
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this run fixed no value under that artifact's source",
    );
    expect(screen.getByRole("button", { name: "Record" })).toBeEnabled();
  });

  it("tells the person why a changed submission under the same id is refused", async () => {
    server.use(
      http.post(REVIEW_ROUTE, () =>
        HttpResponse.json(
          { ok: false, message: "review 'r1' already names a different request, run or route source" },
          { status: 409 },
        ),
      ),
    );
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={vi.fn()} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already names a different request, run or route source",
    );
  });

  it("does not close on Esc while a submission is in flight, so its answer is not thrown away", async () => {
    server.use(
      http.post(REVIEW_ROUTE, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );
    const onClose = vi.fn();
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={onClose} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Record" }));
    expect(await screen.findByRole("button", { name: "Recording" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("leaves without recording anything when the person cancels", async () => {
    const sent: Submission[] = [];
    recording(sent);
    const onClose = vi.fn();
    render(<OwnerReviewForm runId="run-1" refs={[DRAFT]} onClose={onClose} />);

    await fillIn("Alex", "Accepted", "It answered the question.");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(0);
  });
});
