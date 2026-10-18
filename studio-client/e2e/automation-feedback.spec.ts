import { type Page, type Route, expect, test } from "@playwright/test";

import type { z } from "zod";

import type {
  ArtifactRefSchema,
  OwnerReviewSchema,
  RunEnvelopeSchema,
  RunLimitsSchema,
  WorkflowDetailSchema,
  WorkflowGraphSchema,
} from "@/api/studio-types";

// A run that finished, and what a person made of it, with no backend. The run completed and its
// gate was approved by a person: facts about the execution, and no claim that anybody wanted what
// came out. The owner reads it, accepts it, and later rejects a newer value of the same work. Both
// readings stay on the record, in the order they were given.
//
// The fixtures are annotated with the INPUT type of the schema the app parses them with, so a
// fixture that drifts from the contract fails the typecheck rather than the page.

type Wire<S extends { _zod: { input: unknown } }> = z.input<S>;

const WORKFLOW_ID = "wf.weekly-sweep.v1";
const RUN_ID = "run-1";
const FIRST_DIGEST = "a".repeat(64);
const LATER_DIGEST = "b".repeat(64);
const REVIEWER = "Alex";
const PRAISE = "It named both queues and said which one moved.";
const CRITICISM = "The rewritten section dropped the second queue entirely.";
const STALE = "the archived value no longer matches the digest this reference addresses";

function json(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

const GRAPH: Wire<typeof WorkflowGraphSchema> = {
  name: "wf.weekly-sweep",
  version: 1,
  nodes: [
    {
      key: "compose",
      id: "compose",
      kind: "run",
      signature: "",
      detail: "",
      output_schema: null,
      is_output: true,
    },
    { key: "approve", id: "approve", kind: "gate", signature: "", detail: "", output_schema: null },
  ],
  edges: [{ from: "compose", to: "approve", kind: "wire" }],
};

const LIMITS: Wire<typeof RunLimitsSchema> = {
  brakes: [{ window: "run", usd: "2", warn_at: null }],
  gate_modes: ["wait", "orchestrate", "auto"],
  default_gate_mode: "wait",
};

const DETAIL: Wire<typeof WorkflowDetailSchema> = {
  ok: true,
  id: WORKFLOW_ID,
  name: WORKFLOW_ID,
  version: 1,
  description: "Sweeps the open queue every week and writes up what changed.",
  title: "Weekly sweep",
  summary: "Checks the queue every week and writes up what changed.",
  applicability: "",
  provisional: false,
  graph: GRAPH,
  issues: [],
  fits: true,
  missing: [],
  brings: [],
  spec_yaml: "name: wf.weekly-sweep\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
  trust: {
    level: "unverified",
    runs_with_evidence: 0,
    failed_runs: 0,
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
  presentation: {
    purpose: "Keeps the queue moving.",
    produces: "A short write up of what changed.",
    owner_participation: "You read the write up.",
    not_for: "Anything that has to land the same hour.",
    inputs: [],
    result_fields: [],
    actions: [
      {
        kind: "record_owner_review",
        id: "record-my-reading",
        label: "Record my reading",
        artifact_ids: ["write-up"],
      },
    ],
  },
};

function artifact(digest: string): Wire<typeof ArtifactRefSchema> {
  return {
    id: "write-up",
    label: "The weekly write up",
    run_id: RUN_ID,
    node_id: "compose",
    turn: null,
    value_digest: digest,
    media_type: "text/markdown",
  };
}

function endedRun(digest: string): Wire<typeof RunEnvelopeSchema> {
  return {
    run_id: RUN_ID,
    workflow_id: WORKFLOW_ID,
    run_title: "Weekly sweep, the open queue",
    outcome: "completed",
    failure: "",
    started_at: "2026-09-16T09:00:00Z",
    ended_at: "2026-09-16T09:06:00Z",
    runner: "app:harness",
    session_id: "s-run",
    params: { queue: "the open queue" },
    gates_approved: ["approve"],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    result: {
      status: "succeeded",
      summary: "Both sections were written.",
      attempted: 1,
      succeeded: 1,
      skipped: 0,
      failed: 0,
      retry_attempts: 0,
      primary_input: 1,
      generation: 0,
      checks: [],
      artifacts: [artifact(digest)],
    },
  };
}

interface Submission {
  review_id: string;
  actor: string;
  outcome: "accepted" | "rejected";
  reason: string;
  artifact_refs: Wire<typeof ArtifactRefSchema>[];
}

interface Feedback {
  /** The value the run's own record currently names, which a repair can move on. */
  digest: string;
  /** Every review the host has written, oldest first. It only ever grows. */
  reviews: Wire<typeof OwnerReviewSchema>[];
  /** Every submission that reached the host, as posted. */
  submissions: Submission[];
  /** True while the host writes the next review and then loses its answer on the way back. */
  dropAnswer: boolean;
}

function freshFeedback(): Feedback {
  return { digest: FIRST_DIGEST, reviews: [], submissions: [], dropAnswer: false };
}

// Whether two submissions under one review id are the same event. The host decides this from the
// whole request, so this does too: who decided, what they decided, why, and over which exact
// references.
function sameSubmission(held: Wire<typeof OwnerReviewSchema>, sent: Submission): boolean {
  return (
    held.actor === sent.actor &&
    held.outcome === sent.outcome &&
    held.reason === sent.reason &&
    JSON.stringify(held.artifact_refs) === JSON.stringify(sent.artifact_refs)
  );
}

// The host's own rule: a review id names one event. The same id over the same content meets that
// event again, the same id over anything else is refused, and a new id appends beside it.
function recordReview(feedback: Feedback, submission: Submission): [number, unknown] {
  feedback.submissions.push(submission);
  const held = feedback.reviews.find((review) => review.review_id === submission.review_id);
  if (held !== undefined) {
    if (!sameSubmission(held, submission)) {
      return [
        409,
        {
          ok: false,
          message: `review '${submission.review_id}' already names a different request, run or route source`,
        },
      ];
    }
    return [200, { review: held, created: false }];
  }
  const minted: Wire<typeof OwnerReviewSchema> = {
    ...submission,
    source: "app:harness",
    at: `2026-09-16T1${feedback.reviews.length}:00:00Z`,
  };
  feedback.reviews.push(minted);
  return [200, { review: minted, created: true }];
}

async function mockApi(page: Page, feedback: Feedback): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path.startsWith("/api/events")) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "retry: 100000\n\n",
      });
    }
    if (path === `/api/runs/history/${RUN_ID}/review` && method === "POST") {
      const [status, body] = recordReview(feedback, (await request.postDataJSON()) as Submission);
      if (feedback.dropAnswer) {
        // Written, and the answer never arrives. All the sender knows is that it does not know.
        feedback.dropAnswer = false;
        return route.abort("connectionfailed");
      }
      return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    }
    if (path.endsWith("/artifacts/resolve")) {
      // The host answers the reference it was asked about, exactly as it was asked, and refuses
      // one that no longer names the value this run holds. It never hands a newer value back
      // under an older reference.
      const { ref } = (await request.postDataJSON()) as {
        ref: Wire<typeof ArtifactRefSchema>;
      };
      if (ref.value_digest !== feedback.digest) {
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ ok: false, message: STALE }),
        });
      }
      return json(route, { ref, value: "The open queue moved twice this week." });
    }
    if (path.startsWith("/api/runs/history/")) {
      return json(route, {
        ...endedRun(feedback.digest),
        spec: {},
        values: {},
        conversations: {},
        events: [],
        graph: GRAPH,
        owner_reviews: feedback.reviews,
      });
    }
    if (path === "/api/run/limits") return json(route, LIMITS);
    if (path === "/api/runs") return json(route, { runs: [] });
    if (path === "/api/runs/recent") return json(route, { runs: [endedRun(feedback.digest)] });
    if (path === "/api/library") return json(route, []);
    if (path === "/api/master") return json(route, { session_id: "m-1" });
    if (path === "/api/schedules") return json(route, { schedules: [] });
    if (path === "/api/spend") {
      return json(route, {
        available: false,
        scope: "step",
        subject: RUN_ID,
        since_hours: 24,
        reason: "no telemetry here",
        fallback: "the run's own ledger",
      });
    }
    if (path.startsWith("/api/workflow/")) return json(route, DETAIL);
    if (path.endsWith("/history") && path.startsWith("/api/workflows/")) {
      return json(route, { runs: [endedRun(feedback.digest)] });
    }
    if (path === "/api/sessions") return json(route, { sessions: [] });
    return json(route, {});
  });
}

/** A route this walk crosses for the first time is compiled on demand, so crossing one waits longer. */
const CROSSING = { timeout: 30_000 };

test("a completed run is unreviewed until somebody reviews it, and every reading it gets survives", async ({
  page,
}) => {
  const feedback = freshFeedback();
  await mockApi(page, feedback);

  // 1. A run that completed, whose gate a person approved. Three separate things are said about
  //    it: what the work did, who decided its gate, and what anybody made of the result.
  await page.goto(`/workflows/${WORKFLOW_ID}?run=${RUN_ID}`);
  const result = page.locator('[data-section="result"]');
  const execution = page.locator('[data-section="execution-evidence"]');
  const ownerReview = page.locator('[data-section="owner-review"]');
  await expect(result).toContainText("Succeeded", CROSSING);
  await expect(execution).toContainText("Who decided each gate");
  await expect(execution).toContainText("Approved by a person.");

  // 2. Nobody has reviewed it, so its acceptance is unknown. Finishing is not being wanted.
  await expect(ownerReview).toContainText("Not reviewed");
  await expect(ownerReview).toContainText("whether it was any use is unknown");
  await expect(ownerReview).not.toContainText("Accepted");
  await expect(page.getByText(/useful/i)).toHaveCount(0);
  // Not reviewing it costs the reader nothing: the run's own controls stay where they were.
  await expect(page.getByRole("button", { name: "Run again" })).toBeEnabled();

  // 3. Opening the form to see what would be signed, then backing out. Having looked is not
  //    having reviewed, and it leaves the action exactly as it was offered.
  await page.getByRole("button", { name: "Record my reading" }).click();
  await expect(page.getByRole("dialog")).toContainText("The weekly write up");
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(ownerReview).toContainText("Not reviewed");
  expect(feedback.reviews).toHaveLength(0);

  // 4. Reading it and accepting it, over the exact value that is on the page.
  await expect(page.getByRole("button", { name: "Record my reading" })).toBeEnabled();
  await page.getByRole("button", { name: "Record my reading" }).click();
  const form = page.getByRole("dialog");
  await expect(form).toContainText("The weekly write up");
  await expect(form).toContainText(FIRST_DIGEST.slice(0, 12));
  await form.getByLabel("Your name").fill(REVIEWER);
  // The verdict is a radio card: a person presses the card, which is the label over the input.
  await form.getByText("Accepted", { exact: true }).click();
  await expect(form.getByRole("radio", { name: "Accepted" })).toBeChecked();
  await form.getByLabel("Why").fill(PRAISE);
  feedback.dropAnswer = true;
  await form.getByRole("button", { name: "Record" }).click();
  // The host wrote it and the answer never came back, so the person is left not knowing.
  await expect(form.getByRole("alert")).toBeVisible();

  // 5. Having second thoughts and sending the opposite verdict under that same id is refused:
  //    the reading is already on record, and what is on record is not edited from here.
  await form.getByText("Rejected", { exact: true }).click();
  await expect(form.getByRole("radio", { name: "Rejected" })).toBeChecked();
  await form.getByRole("button", { name: "Record" }).click();
  await expect(form.getByRole("alert")).toContainText("already names a different request");

  // 6. Sending again exactly what was sent before meets the event that first send wrote, and adds
  //    nothing to it.
  await form.getByText("Accepted", { exact: true }).click();
  await form.getByRole("button", { name: "Record" }).click();
  await expect(form.getByRole("status")).toContainText("already recorded");
  await form.getByRole("button", { name: "Close" }).click();
  expect(feedback.submissions).toHaveLength(3);
  expect(new Set(feedback.submissions.map((sent) => sent.review_id)).size).toBe(1);

  await expect(ownerReview).toContainText("Accepted");
  await expect(ownerReview).toContainText(REVIEWER);
  await expect(ownerReview).toContainText(PRAISE);
  await expect(ownerReview).toContainText(FIRST_DIGEST.slice(0, 12));
  expect(feedback.reviews).toHaveLength(1);

  // 7. Having reviewed it once does not close the question: the action is there to be pressed
  //    again, which is how a second reading gets recorded.
  await expect(page.getByRole("button", { name: "Record my reading" })).toBeEnabled();

  // 8. The work is done again and the value moves on while this page still shows the old one.
  //    Pressing the action now reaches a reference the run no longer holds, and the host says so
  //    rather than opening a form over a value nobody here has read. Nothing is recorded, and the
  //    action is still there to press.
  feedback.digest = LATER_DIGEST;
  await page.getByRole("button", { name: "Record my reading" }).click();
  const artifacts = page.locator('[data-section="artifacts"]');
  await expect(artifacts.getByRole("alert")).toContainText(STALE);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(feedback.reviews).toHaveLength(1);
  await expect(page.getByRole("button", { name: "Record my reading" })).toBeEnabled();

  // 9. Reading the run again brings the value it holds now. The reading that is offered is that
  //    one, and this time it is rejected, with a reason. The first reading is still on the page,
  //    over the value it actually read.
  await page.reload();
  await expect(ownerReview).toContainText(PRAISE, CROSSING);
  await expect(ownerReview).toContainText(FIRST_DIGEST.slice(0, 12));
  await page.getByRole("button", { name: "Record my reading" }).click(CROSSING);
  const second = page.getByRole("dialog");
  await expect(second).toContainText(LATER_DIGEST.slice(0, 12));
  await second.getByLabel("Your name").fill(REVIEWER);
  await second.getByText("Rejected", { exact: true }).click();
  await expect(second.getByRole("radio", { name: "Rejected" })).toBeChecked();
  await second.getByLabel("Why").fill(CRITICISM);
  await second.getByRole("button", { name: "Record" }).click();
  await expect(second.getByRole("status")).toContainText("Recorded.");
  await second.getByRole("button", { name: "Close" }).click();

  // 10. Two readings, in the order they were given. The rejection is another event, not a rewrite
  //    of the acceptance: the first still names what it read, in its own words.
  await expect(ownerReview).toContainText("Rejected");
  await expect(ownerReview).toContainText(CRITICISM);
  await expect(ownerReview).toContainText(LATER_DIGEST.slice(0, 12));
  await expect(ownerReview).toContainText(PRAISE);
  await expect(ownerReview).toContainText(FIRST_DIGEST.slice(0, 12));
  expect(feedback.reviews).toHaveLength(2);
  expect(feedback.reviews[0]).toMatchObject({ outcome: "accepted", reason: PRAISE });
  expect(feedback.reviews[1]).toMatchObject({ outcome: "rejected", reason: CRITICISM });
  expect(feedback.reviews[0]?.review_id).not.toBe(feedback.reviews[1]?.review_id);
  expect(feedback.reviews[0]?.artifact_refs[0]?.value_digest).toBe(FIRST_DIGEST);
  expect(feedback.reviews[1]?.artifact_refs[0]?.value_digest).toBe(LATER_DIGEST);

  // 11. Neither reading touched what the run itself did, which is still what its own facts say.
  await expect(result).toContainText("Succeeded");
});
