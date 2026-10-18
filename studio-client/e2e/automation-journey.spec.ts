import { type Page, type Route, expect, test } from "@playwright/test";

import type { z } from "zod";

import type {
  ArtifactRefSchema,
  OwnerReviewSchema,
  RunEnvelopeSchema,
  RunLimitsSchema,
  WorkflowDetailSchema,
  WorkflowGraphSchema,
  WorkflowPresentationSchema,
} from "@/api/studio-types";
import type { NotePreviewSchema, RetainedNoteSchema } from "@/api/types";

// The whole automation journey as one person walks it, with no backend anywhere: ask for an
// automation in ordinary words, read what was proposed, save it, be refused a value the workflow
// will not take, start it deliberately, read a run a reviewer refused once and then accepted, keep
// what it worked out as a note in a knowledge base of your choosing, record your own reading of it,
// run it again on changed values, and make the next version out of the run that disappointed. Two
// shorter walks sit beside it: a run that skipped every item it was handed, and a value that moved
// on between reading it and keeping it.
//
// Two hosts answer here. The harness serves the run on this origin, and the knowledge base API
// serves the keep on its own. Every request is routed by which of the two it is addressed to, so a
// keep can never be answered by the run's host or the other way round.
//
// Every assertion names what the person sees or what a host now holds. The fixtures are annotated
// with the INPUT type of the schema the app parses them with, so a fixture that drifts from the
// contract fails the typecheck rather than the page.

type Wire<S extends { _zod: { input: unknown } }> = z.input<S>;

const KNOWLEDGE_BASE_ORIGIN = "http://api.test";

const WORKFLOW_ID = "wf.file-questions.v1";
const DESIGN_SESSION = "s-design";
const RUN_SESSION = "s-run";
const ADAPT_SESSION = "s-adapt";
const RUN_ID = "run-1";
const SWEEP_RUN_ID = "run-swept";

const REQUEST = "Every week, file each open question from the reviews as its own page";
const ASSUMPTION = "The reviews to read are the ones this session already names";
const RUN_TITLE = "File the open questions, this week";
const TOPIC = "what the reviews left open";
const REFUSED_TOPIC = "everything, all of it, every page in the repository";
const REFUSAL = "topic: $ is longer than the 40 characters this parameter declares";

const NOTE_DIGEST = "a".repeat(64);
const PAGE_DIGEST = "b".repeat(64);
const MOVED_DIGEST = "c".repeat(64);
const SCHEMA_DIGEST = "d".repeat(64);
const PREVIEW_DIGEST = "e".repeat(64);
const SECOND_PREVIEW_DIGEST = "f".repeat(64);

const DESTINATION = "field-notes";
const NOTE_PATH = "questions/what-the-reviews-left-open.md";
const REVIEWER = "Alex";
const PRAISE = "Both questions are on the page, in the words the review used.";
const STALE = "the archived value no longer matches the digest this reference addresses";

function json(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

function refuse(route: Route, status: number, message: string) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify({ ok: false, message }),
  });
}

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  return `${chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("")}data: [DONE]\n\n`;
}

const GRAPH: Wire<typeof WorkflowGraphSchema> = {
  name: "wf.file-questions",
  version: 1,
  nodes: [
    {
      key: "topic",
      id: "topic",
      kind: "param",
      signature: "string",
      detail: "What the pages are about.",
      output_schema: { type: "string" },
      required: true,
    },
    {
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "",
      detail: "",
      output_schema: null,
    },
    { key: "check", id: "check", kind: "judge", signature: "", detail: "", output_schema: null },
    {
      key: "filed",
      id: "filed",
      kind: "run",
      signature: "",
      detail: "",
      output_schema: null,
      is_output: true,
    },
  ],
  edges: [
    { from: "topic", to: "draft", kind: "wire" },
    { from: "draft", to: "check", kind: "wire" },
    { from: "check", to: "filed", kind: "wire" },
  ],
};

const READY = { executable: true, described: true, findings: [] };

const LIMITS: Wire<typeof RunLimitsSchema> = {
  brakes: [{ window: "run", usd: "2", warn_at: null }],
  gate_modes: ["wait", "orchestrate", "auto"],
  default_gate_mode: "wait",
};

const PRESENTATION: Wire<typeof WorkflowPresentationSchema> = {
  purpose: "Keeps the open questions from a review out of anybody's head.",
  produces: "One page per question, and a note you may keep.",
  owner_participation: "You read the page and decide whether to keep the note.",
  not_for: "Anything that has to land the same hour.",
  inputs: [{ key: "topic", label: "What the pages are about", control: "text" }],
  result_fields: [],
  actions: [
    {
      kind: "save_note",
      id: "keep-the-note",
      label: "Keep as a note",
      artifact_id: "note_draft",
      destination: "user_selects",
    },
    {
      kind: "record_owner_review",
      id: "record-my-reading",
      label: "Record my reading",
      artifact_ids: ["page"],
    },
  ],
};

const DETAIL: Wire<typeof WorkflowDetailSchema> = {
  ok: true,
  id: WORKFLOW_ID,
  name: WORKFLOW_ID,
  version: 1,
  description: "Draft a page for each open question, have it reviewed, and file what was accepted.",
  title: "File the open questions",
  summary: "Drafts a page per open question and files what the reviewer accepted.",
  applicability: "",
  provisional: false,
  graph: GRAPH,
  issues: [],
  fits: true,
  missing: [],
  brings: [],
  spec_yaml: "name: wf.file-questions\n",
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
  presentation: PRESENTATION,
};

const DRAFTED = {
  spec_yaml: "name: wf.file-questions\nversion: 1\n",
  title: "File the open questions",
  summary: "Drafts a page per open question and files what the reviewer accepted.",
  applicability: "Use at the end of a review week.",
  lineage: null,
  presentation: PRESENTATION,
  intent: {
    request: REQUEST,
    expected_result: "One page per open question, and a note I may keep.",
    success_example: "",
    empty_input_behavior: "",
    failure_behavior: "",
    trigger: "scheduled",
    assumptions: [ASSUMPTION],
  },
};

const ADAPTED_DESCRIPTION = "File each open question, and say which review it came from.";

const CHILD_DRAFT = {
  ...DRAFTED,
  lineage: {
    parent_name: "wf.file-questions",
    parent_version: 1,
    touched: [],
    ops: [{ op: "set_description", to: ADAPTED_DESCRIPTION }],
    evidence: [
      `Adapted from run ${RUN_ID} of ${WORKFLOW_ID}.`,
      "Observed outcome: succeeded. Both questions were filed.",
      "What the owner asked for: it never said which review a question came from.",
    ].join("\n"),
  },
};

function artifact(id: string, label: string, digest: string): Wire<typeof ArtifactRefSchema> {
  return {
    id,
    label,
    run_id: RUN_ID,
    node_id: "filed",
    turn: null,
    value_digest: digest,
    media_type: id === "page" ? "text/markdown" : "application/json",
  };
}

const PROPOSED_NOTE = {
  page_type: "question",
  title: TOPIC,
  summary: "Two questions came out of this review.",
  frontmatter: { question_status: "open" },
  sections: [
    { heading: "What was asked", body: "Who owns the retention window." },
    { heading: "What settles it", body: "The decision record nobody has written yet." },
  ],
};

// The run the person reads: it completed, its reviewer refused the first draft and accepted the
// repair, and the counts stay counts. One rework, and still one piece of work.
function filedRun(noteDigest: string): Wire<typeof RunEnvelopeSchema> {
  return {
    run_id: RUN_ID,
    workflow_id: WORKFLOW_ID,
    run_title: RUN_TITLE,
    outcome: "completed",
    failure: "",
    started_at: "2026-09-16T09:00:00Z",
    ended_at: "2026-09-16T09:06:00Z",
    runner: "app:harness",
    session_id: RUN_SESSION,
    params: { topic: TOPIC },
    gates_approved: [],
    gates_rejected: { check: 1 },
    gates_auto: [],
    gates_orchestrated: ["check"],
    pinned: false,
    result: {
      status: "succeeded",
      summary: "Both questions were filed.",
      attempted: 1,
      succeeded: 1,
      skipped: 0,
      failed: 0,
      retry_attempts: 1,
      primary_input: 1,
      generation: 0,
      checks: [],
      artifacts: [
        artifact("note_draft", "The note this run proposes", noteDigest),
        artifact("page", "The page this run wrote", PAGE_DIGEST),
      ],
    },
  };
}

// The other run: every page it was handed was left out. Nothing failed and nothing succeeded, which
// is the reading most likely to be mistaken for a quiet success.
function sweptRun(): Wire<typeof RunEnvelopeSchema> {
  return {
    run_id: SWEEP_RUN_ID,
    workflow_id: WORKFLOW_ID,
    run_title: "Sweep the reference pages",
    outcome: "completed",
    failure: "",
    started_at: "2026-09-16T10:00:00Z",
    ended_at: "2026-09-16T10:02:00Z",
    runner: "app:harness",
    session_id: RUN_SESSION,
    params: { topic: TOPIC },
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    result: {
      status: "failed",
      summary: "Every page was left out.",
      attempted: 3,
      succeeded: 0,
      skipped: 3,
      failed: 0,
      retry_attempts: 0,
      primary_input: 3,
      generation: 0,
      checks: [],
      artifacts: [],
    },
  };
}

function session(id: string, title: string, kind = "chat") {
  return {
    id,
    project_id: "",
    title,
    started_at: "2026-09-16T09:00:00Z",
    last_activity_at: "2026-09-16T09:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind,
    cost_usd: "0",
    context: null,
  };
}

interface RetainRequest {
  idempotency_key: string;
  origin: Wire<typeof ArtifactRefSchema>;
  draft: typeof PROPOSED_NOTE;
  schema_digest: string;
  preview_digest: string;
}

interface Journey {
  /** True once the design turn has settled, which is when the proposal exists. */
  drafted: boolean;
  saved: boolean;
  /** The run the workflow page shows, or null while nothing has run. */
  shown: Wire<typeof RunEnvelopeSchema> | null;
  /** The digest the run's own record currently names for the note it proposed. */
  noteDigest: string;
  /** Which values the workflow's host will not admit, and what it says about each. */
  refuses: Record<string, string>;
  /** Every run start that reached the run's host, as posted. */
  launched: Record<string, unknown>[];
  /** Every adaptation that reached the run's host, as posted. */
  adapted: Record<string, unknown>[];
  /** Every schedule write that reached the run's host. Designing must leave this empty. */
  scheduled: Record<string, unknown>[];
  /** Every owner review the run's host holds, oldest first. */
  reviews: Wire<typeof OwnerReviewSchema>[];
  /** Every preview the knowledge base was asked for, as posted. */
  previewed: { name: string; body: Record<string, unknown> }[];
  /** Every keep that reached the knowledge base, as posted. */
  kept: { name: string; body: RetainRequest }[];
  /** What each keep is answered with, in order. The last one repeats once it runs out. */
  keepAnswers: Wire<typeof RetainedNoteSchema>["outcome"][];
}

function freshJourney(): Journey {
  return {
    drafted: false,
    saved: false,
    shown: null,
    noteDigest: NOTE_DIGEST,
    refuses: {},
    launched: [],
    adapted: [],
    scheduled: [],
    reviews: [],
    previewed: [],
    kept: [],
    keepAnswers: ["saved"],
  };
}

function designTurn(): StreamChunk[] {
  const said = "Here is the shape it would run. Read it and tell me what to change.";
  return [
    { type: "start" },
    { type: "start-step" },
    { type: "text-start", id: "t1" },
    { type: "text-delta", id: "t1", delta: said },
    { type: "text-end", id: "t1" },
    {
      type: "data-settle",
      data: {
        message: said,
        draft: DRAFTED,
        issues: [],
        valid: true,
        graph: GRAPH,
        readiness: READY,
        intent_issues: ["success_example", "empty_input_behavior", "failure_behavior"],
      },
    },
    { type: "finish-step" },
    { type: "finish" },
  ];
}

function conversation(journey: Journey, sessionId: string) {
  if (sessionId === ADAPT_SESSION) {
    return {
      turns: [],
      draft: CHILD_DRAFT,
      issues: [],
      valid: true,
      graph: GRAPH,
      readiness: READY,
      intent_issues: [],
    };
  }
  if (!journey.drafted) {
    return {
      turns: [],
      draft: { spec_yaml: "", title: "", summary: "", applicability: "" },
      issues: [],
      valid: false,
      graph: null,
      readiness: null,
      intent_issues: [],
    };
  }
  return {
    turns: [{ who: "you", text: REQUEST }],
    draft: DRAFTED,
    issues: [],
    valid: true,
    graph: GRAPH,
    readiness: READY,
    intent_issues: ["success_example", "empty_input_behavior", "failure_behavior"],
  };
}

const KNOWLEDGE_BASES = {
  knowledge_bases: [
    {
      name: DESTINATION,
      registered: true,
      indexed: true,
      broken: false,
      path: "/knowledge/field-notes",
      description: "Where the questions go.",
      pages: 12,
      page_types: ["question"],
      last_indexed: "2026-09-16T08:00:00Z",
    },
    {
      name: "not-registered-here",
      registered: false,
      indexed: false,
      broken: false,
      path: null,
      description: "",
      pages: null,
      page_types: [],
      last_indexed: null,
    },
  ],
};

function preview(digest: string): Wire<typeof NotePreviewSchema> {
  return {
    knowledge_base: DESTINATION,
    note_id: "question-what-the-reviews-left-open",
    draft: PROPOSED_NOTE,
    markdown: `# ${TOPIC}\n`,
    path: NOTE_PATH,
    read_ref: `${RUN_ID}:note_draft`,
    schema_digest: SCHEMA_DIGEST,
    preview_digest: digest,
    warnings: [],
  };
}

// The knowledge base's own half: it lists what is registered, says what one value would become,
// and writes only what a keep carries back to it.
async function mockKnowledgeBaseApi(page: Page, journey: Journey): Promise<void> {
  await page.route(`${KNOWLEDGE_BASE_ORIGIN}/api/**`, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === "/api/knowledge-bases" && method === "GET") return json(route, KNOWLEDGE_BASES);
    if (path.endsWith("/notes/preview") && method === "POST") {
      const body = (await request.postDataJSON()) as {
        origin: Wire<typeof ArtifactRefSchema>;
        edited_draft?: typeof PROPOSED_NOTE;
      };
      const name = decodeURIComponent(path.split("/")[3] ?? "");
      journey.previewed.push({ name, body });
      if (body.origin.value_digest !== journey.noteDigest) {
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ error: "origin_unavailable", detail: STALE }),
        });
      }
      const edited = body.edited_draft;
      const answered =
        edited === undefined
          ? preview(PREVIEW_DIGEST)
          : { ...preview(SECOND_PREVIEW_DIGEST), draft: edited };
      return json(route, answered);
    }
    if (path.endsWith("/notes") && method === "POST") {
      const body = (await request.postDataJSON()) as RetainRequest;
      const name = decodeURIComponent(path.split("/")[3] ?? "");
      journey.kept.push({ name, body });
      const outcome = journey.keepAnswers.shift() ?? "already_indexed";
      if (journey.keepAnswers.length === 0) journey.keepAnswers.push(outcome);
      const retained: Wire<typeof RetainedNoteSchema> = {
        outcome,
        knowledge_base: name,
        note_id: "question-what-the-reviews-left-open",
        path: NOTE_PATH,
        read_ref: `${RUN_ID}:note_draft`,
        detail: "",
        warnings: [],
      };
      return json(route, retained);
    }
    return json(route, {});
  });
}

// The run's own host. Everything a walk can ask it, answered from one mutable journey.
async function mockRunApi(page: Page, journey: Journey): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === KNOWLEDGE_BASE_ORIGIN) return route.fallback();
    const path = url.pathname;
    const method = request.method();

    if (path.startsWith("/api/events")) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "retry: 100000\n\n",
      });
    }
    if (path.endsWith("/steer") && method === "POST") {
      return refuse(route, 409, "no turn is in flight");
    }
    if (path.endsWith("/messages") && method === "POST") {
      journey.drafted = true;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: sseBody(designTurn()),
      });
    }
    if (path.endsWith("/adapt") && method === "POST") {
      journey.adapted.push((await request.postDataJSON()) as Record<string, unknown>);
      return json(route, { ok: true });
    }
    if (path.endsWith("/save") && method === "POST") {
      journey.saved = true;
      return json(route, { ok: true, id: WORKFLOW_ID });
    }
    if (path === "/api/run/limits") return json(route, LIMITS);
    if (path === "/api/runs") return json(route, { runs: [] });
    if (path === "/api/runs/recent") {
      return json(route, { runs: journey.shown === null ? [] : [journey.shown] });
    }
    if (path === "/api/library") return json(route, []);
    if (path === "/api/master") return json(route, { session_id: "m-1" });
    if (path === "/api/schedules" && method !== "GET") {
      journey.scheduled.push((await request.postDataJSON()) as Record<string, unknown>);
      return json(route, { ok: true });
    }
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
      return json(route, { runs: journey.shown === null ? [] : [journey.shown] });
    }
    if (path.endsWith("/artifacts/resolve") && method === "POST") {
      const { ref } = (await request.postDataJSON()) as {
        ref: Wire<typeof ArtifactRefSchema>;
      };
      if (ref.id === "note_draft" && ref.value_digest !== journey.noteDigest) {
        return refuse(route, 409, STALE);
      }
      const value = ref.id === "note_draft" ? PROPOSED_NOTE : `# ${TOPIC}\n`;
      return json(route, { ref, value });
    }
    if (path.endsWith("/review") && method === "POST") {
      const sent = (await request.postDataJSON()) as Wire<typeof OwnerReviewSchema>;
      const minted = {
        ...sent,
        source: "app:harness",
        at: `2026-09-16T1${journey.reviews.length}:00:00Z`,
      };
      journey.reviews.push(minted);
      return json(route, { review: minted, created: true });
    }
    if (path.startsWith("/api/runs/history/")) {
      const envelope = journey.shown ?? filedRun(journey.noteDigest);
      return json(route, {
        ...envelope,
        spec: {},
        values: {},
        conversations: {},
        events: [],
        graph: GRAPH,
        owner_reviews: journey.reviews,
      });
    }
    if (path.endsWith("/conversation")) {
      return json(route, conversation(journey, path.split("/")[3] ?? ""));
    }
    if (path.endsWith("/run/state")) {
      return json(route, {
        running: false,
        workflow: null,
        result: null,
        active: [],
        done: [],
        failed: [],
        waiting_at: null,
        events: [],
        seq: 0,
        auto_approve: false,
        gate_mode: "wait",
      });
    }
    if (path.endsWith("/run") && method === "POST") {
      const body = (await request.postDataJSON()) as { arguments?: Record<string, unknown> };
      const topic = String(body.arguments?.topic ?? "");
      const refusal = journey.refuses[topic];
      if (refusal !== undefined) return refuse(route, 400, refusal);
      journey.launched.push(body as Record<string, unknown>);
      return json(route, { ok: true, running: WORKFLOW_ID });
    }
    if (path === "/api/sessions" && method === "POST") {
      const asked = (await request.postDataJSON()) as { kind?: string };
      const id = asked.kind === "runner" ? RUN_SESSION : ADAPT_SESSION;
      return json(route, { session: session(id, "", asked.kind ?? "chat") });
    }
    if (path === "/api/sessions") return json(route, { sessions: [] });
    if (path.startsWith("/api/sessions/") && method === "PATCH") {
      return json(route, { session: session(path.split("/")[3] ?? "", "renamed") });
    }
    if (path === `/api/sessions/${DESIGN_SESSION}`) {
      return json(route, { session: session(DESIGN_SESSION, "Designing on a Monday"), turns: [] });
    }
    if (path === "/api/sessions/m-1") {
      return json(route, { session: session("m-1", "master", "master"), turns: [] });
    }
    return json(route, {});
  });
}

async function mockBothHosts(page: Page, journey: Journey): Promise<void> {
  await mockRunApi(page, journey);
  await mockKnowledgeBaseApi(page, journey);
}

/** A route this walk crosses for the first time is compiled on demand, so crossing one waits longer. */
const CROSSING = { timeout: 30_000 };

test("from a plain request to a run, a kept note, a recorded reading, a rerun and the next version", async ({
  page,
}) => {
  const journey = freshJourney();
  journey.refuses[REFUSED_TOPIC] = REFUSAL;
  journey.keepAnswers = ["saved_not_indexed", "already_indexed"];
  await mockBothHosts(page, journey);

  // 1. Asking for it in ordinary words. Designing designs, and reaches nothing that starts work.
  await page.goto(`/workflows/draft?session=${DESIGN_SESSION}`);
  await expect(
    page.getByRole("heading", { name: "What should this automation do?" }),
  ).toBeVisible(CROSSING);
  await page.getByLabel("What should this automation do?").fill(REQUEST);
  await page.getByRole("button", { name: "Design it" }).click();

  await expect(page.getByRole("heading", { name: "What you asked for" })).toBeVisible(CROSSING);
  await expect(page.getByText(REQUEST).first()).toBeVisible();
  await expect(page.locator(`input[value="${ASSUMPTION}"]`)).toBeVisible();
  expect(journey.launched).toHaveLength(0);
  expect(journey.scheduled).toHaveLength(0);

  // 2. Saving keeps it in the library and still starts nothing.
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(`Saved as ${WORKFLOW_ID}`)).toBeVisible();
  expect(journey.saved).toBe(true);
  expect(journey.launched).toHaveLength(0);
  expect(journey.scheduled).toHaveLength(0);

  // 3. A start the workflow will not take. The host's own sentence is shown, and nothing ran.
  await page.goto(`/workflows/${WORKFLOW_ID}`);
  await page.getByRole("button", { name: "Run", exact: true }).click(CROSSING);
  const setup = page.getByRole("dialog");
  await expect(setup).toBeVisible();
  await expect(setup.getByText("$2 per run")).toBeVisible();
  await setup.getByRole("button", { name: "Run it" }).click();
  await expect(setup.getByRole("alert")).toContainText("1 field needs an answer.");
  expect(journey.launched).toHaveLength(0);

  // 4. A value that is filled in and still refused. The host's own sentence is shown, and nothing ran.
  await setup.getByLabel("What the pages are about").fill(REFUSED_TOPIC);
  await setup.getByRole("button", { name: "Run it" }).click();
  await expect(setup.getByText(REFUSAL)).toBeVisible();
  expect(journey.launched).toHaveLength(0);

  // 5. The same start with a value the workflow takes: one deliberate launch, carrying it exactly.
  await setup.getByLabel("What the pages are about").fill(TOPIC);
  await setup.getByRole("button", { name: "Run it" }).click();
  await expect.poll(() => journey.launched.length).toBe(1);
  expect(journey.launched[0]).toMatchObject({
    workflow_id: WORKFLOW_ID,
    arguments: { topic: TOPIC },
  });

  // 6. What the run left behind: a reviewer refused the first draft and accepted the repair, and
  //    the rework never becomes a second piece of work.
  journey.shown = filedRun(journey.noteDigest);
  await page.goto(`/workflows/${WORKFLOW_ID}?run=${RUN_ID}`);
  const result = page.locator('[data-section="result"]');
  const artifacts = page.locator('[data-section="artifacts"]');
  const ownerReview = page.locator('[data-section="owner-review"]');
  await expect(result).toContainText("Succeeded", CROSSING);
  await expect(result).toContainText("1 of 1 succeeded, checked against the run's own facts.");
  await expect(artifacts).toContainText("The note this run proposes");
  await expect(artifacts).toContainText("The page this run wrote");

  // 7. Nobody has reviewed it yet, so whether it was any use is unknown.
  await expect(ownerReview).toContainText("Not reviewed");
  await expect(ownerReview).toContainText("whether it was any use is unknown");

  // 8. Keeping what it worked out, as a note. Opening the form writes nothing at all.
  await artifacts.getByRole("button", { name: "Keep as a note" }).click();
  const keeping = page.getByRole("dialog");
  await expect(keeping).toContainText("Keep this as a note");
  await expect(keeping).toContainText("Nothing is written until you do.");
  expect(journey.previewed).toHaveLength(0);
  expect(journey.kept).toHaveLength(0);

  // A knowledge base nothing here registers is not offered as a destination.
  await expect(keeping.getByRole("option", { name: "not-registered-here" })).toHaveCount(0);
  await keeping.getByLabel("Knowledge base").selectOption(DESTINATION);
  await expect(keeping.getByText(NOTE_PATH)).toBeVisible();
  await expect(keeping.getByLabel("Title")).toHaveValue(TOPIC);
  await expect(keeping.getByLabel("What was asked")).toHaveValue("Who owns the retention window.");
  expect(journey.previewed).toHaveLength(1);
  expect(journey.previewed[0]?.name).toBe(DESTINATION);
  expect(journey.kept).toHaveLength(0);

  // 9. Changing a word means this is no longer the note that was read, so it cannot be kept until
  //    the destination has been asked about the new one.
  await keeping.getByLabel("Title").fill("What this week's reviews left open");
  await expect(keeping.getByRole("status")).toContainText("Take the preview again");
  await expect(keeping.getByRole("button", { name: "Keep" })).toBeDisabled();
  await keeping.getByRole("button", { name: "Preview again" }).click();
  await expect.poll(() => journey.previewed.length).toBe(2);
  expect(journey.previewed[1]?.body).toMatchObject({
    edited_draft: { title: "What this week's reviews left open" },
  });
  await expect(keeping.getByRole("button", { name: "Keep" })).toBeEnabled();

  // 10. Keeping it. The host wrote it and did not index it, so there is one thing left to do, and
  //    doing it again is the same request rather than a second note.
  await keeping.getByRole("button", { name: "Keep" }).click();
  await expect(keeping.getByRole("status")).toContainText("Kept, but not indexed yet");
  await keeping.getByRole("button", { name: "Try indexing again" }).click();
  await expect(keeping.getByRole("status")).toContainText(
    "This exact note was already kept, so nothing was written.",
  );
  expect(journey.kept).toHaveLength(2);
  expect(journey.kept[0]?.name).toBe(DESTINATION);
  expect(journey.kept[0]?.body.idempotency_key).toBe(journey.kept[1]?.body.idempotency_key);
  expect(journey.kept[0]?.body.preview_digest).toBe(SECOND_PREVIEW_DIGEST);
  expect(journey.kept[0]?.body.schema_digest).toBe(SCHEMA_DIGEST);
  expect(journey.kept[0]?.body.origin.value_digest).toBe(NOTE_DIGEST);
  expect(journey.kept[0]?.body.draft.title).toBe("What this week's reviews left open");
  await keeping.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // 11. Recording the person's own reading of the page, over the exact value on the page.
  await artifacts.getByRole("button", { name: "Record my reading" }).click();
  const reviewForm = page.getByRole("dialog");
  await expect(reviewForm).toContainText(PAGE_DIGEST.slice(0, 12));
  await reviewForm.getByLabel("Your name").fill(REVIEWER);
  await reviewForm.getByText("Accepted", { exact: true }).click();
  await reviewForm.getByLabel("Why").fill(PRAISE);
  await reviewForm.getByRole("button", { name: "Record" }).click();
  await expect(reviewForm.getByRole("status")).toContainText("Recorded.");
  await reviewForm.getByRole("button", { name: "Close" }).click();

  await expect(ownerReview).toContainText("Accepted");
  await expect(ownerReview).toContainText(PRAISE);
  await expect(ownerReview).toContainText(PAGE_DIGEST.slice(0, 12));
  expect(journey.reviews).toHaveLength(1);
  expect(journey.reviews[0]?.artifact_refs[0]?.value_digest).toBe(PAGE_DIGEST);
  await expect(result).toContainText("Succeeded");

  // 12. Running it again on what that run was given, with the value changed. Another deliberate start.
  await page.getByRole("button", { name: "Run again" }).click();
  const again = page.getByRole("dialog");
  await expect(again.getByLabel("What the pages are about")).toHaveValue(TOPIC);
  await again.getByLabel("What the pages are about").fill("what the flags left open");
  await again.getByRole("button", { name: "Run it" }).click();
  await expect.poll(() => journey.launched.length).toBe(2);
  expect(journey.launched[1]).toMatchObject({ arguments: { topic: "what the flags left open" } });
  expect(journey.launched[0]).toMatchObject({ arguments: { topic: TOPIC } });

  // 13. The next version, made out of the run that disappointed. The one it came from is unchanged.
  await page.getByRole("button", { name: "Make a new version" }).click();
  const adapting = page.getByRole("dialog");
  await expect(adapting).toContainText(`Adapting from ${RUN_TITLE}`);
  await adapting.getByLabel("What this version does").fill(ADAPTED_DESCRIPTION);
  await adapting.getByLabel("Why").fill("it never said which review a question came from");
  await adapting.getByRole("button", { name: "Make the new version" }).click();

  await expect.poll(() => journey.adapted.length).toBe(1);
  expect(journey.adapted[0]).toEqual({
    workflow_id: WORKFLOW_ID,
    ops: [{ op: "set_description", to: ADAPTED_DESCRIPTION }],
    source_run_id: RUN_ID,
    feedback: "it never said which review a question came from",
  });
  await expect(page).toHaveURL(new RegExp(`session=${ADAPT_SESSION}`), CROSSING);
  const lineage = page.locator('[data-section="adapted-from"]');
  await expect(lineage).toContainText("A new version of wf.file-questions v1");
  await expect(lineage).toContainText("The one it came from is unchanged.");
});

test("a run that skipped every page it was handed reads as work not done", async ({ page }) => {
  const journey = freshJourney();
  journey.shown = sweptRun();
  await mockBothHosts(page, journey);

  await page.goto(`/workflows/${WORKFLOW_ID}?run=${SWEEP_RUN_ID}`);

  const result = page.locator('[data-section="result"]');
  await expect(result).toContainText("Work failed", CROSSING);
  await expect(result).toContainText("0 of 3 failed, 3 skipped, 0 succeeded.");
  await expect(result).not.toContainText("Succeeded");
  // Nothing was left behind, so there is nothing to keep and nothing to review over.
  await expect(page.locator('[data-section="artifacts"]')).toContainText(
    "This run fixed no value for note_draft, so there is nothing to act on.",
  );
  await expect(page.getByRole("button", { name: "Keep as a note" })).toBeDisabled();
});

test("a value the run no longer holds cannot be kept, and keeping it writes nothing", async ({
  page,
}) => {
  const journey = freshJourney();
  journey.shown = filedRun(NOTE_DIGEST);
  await mockBothHosts(page, journey);

  await page.goto(`/workflows/${WORKFLOW_ID}?run=${RUN_ID}`);
  const artifacts = page.locator('[data-section="artifacts"]');
  await expect(artifacts).toContainText("The note this run proposes", CROSSING);

  // The work was done again and the value moved on while this page still shows the old one.
  journey.noteDigest = MOVED_DIGEST;
  await artifacts.getByRole("button", { name: "Keep as a note" }).click();

  await expect(artifacts.getByRole("alert")).toContainText(STALE);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(journey.previewed).toHaveLength(0);
  expect(journey.kept).toHaveLength(0);
  // The action is still there to press: a refusal closed nothing.
  await expect(artifacts.getByRole("button", { name: "Keep as a note" })).toBeEnabled();
});
