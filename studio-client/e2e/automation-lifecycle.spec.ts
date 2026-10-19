import { type Page, type Route, expect, test } from "@playwright/test";

import type { z } from "zod";

import type { RunViewSchema } from "@/api/sessions-types";
import type {
  ArtifactRefSchema,
  RunEnvelopeSchema,
  RunLimitsSchema,
  WorkflowDetailSchema,
  WorkflowGraphSchema,
} from "@/api/studio-types";

// One automation from asked for to reused, with no backend: request it, read the proposal and the
// assumption made on your behalf, save it, fill its typed inputs, launch it deliberately, read the
// artifact the run left, run it again on those values with one of them changed, and make the next
// version out of the run that disappointed.
//
// Every request is answered from one mutable journey, so the same walk can move a workflow from
// drafted to saved to run to adapted. The fixtures are annotated with the INPUT type of the schema
// the app parses them with, so a fixture that drifts from the contract fails the typecheck rather
// than the page.

type Wire<S extends { _zod: { input: unknown } }> = z.input<S>;

const WORKFLOW_ID = "wf.weekly-sweep.v1";
const DESIGN_SESSION = "s-design";
const RUN_SESSION = "s-run";
const ADAPT_SESSION = "s-adapt";

// The session the designing happens in wears a name of its own. No run, and no task, ever takes
// this name: a run is named by its own setup, and this is here to prove that.
const DESIGN_SESSION_TITLE = "Designing things on a Monday";
const REQUEST = "Every Monday, tell me what changed in the open queue";
const ASSUMPTION = "The queue to sweep is the one this session already names";
const RUN_TITLE = "Weekly sweep, the open queue";

function json(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return `${lines}data: [DONE]\n\n`;
}

function designTurn(): StreamChunk[] {
  return [
    { type: "start" },
    { type: "start-step" },
    { type: "text-start", id: "t1" },
    {
      type: "text-delta",
      id: "t1",
      delta: "Here is the shape it would run. Read it and tell me what to change.",
    },
    { type: "text-end", id: "t1" },
    {
      type: "data-settle",
      data: {
        message: "Here is the shape it would run. Read it and tell me what to change.",
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

const GRAPH: Wire<typeof WorkflowGraphSchema> = {
  name: "wf.weekly-sweep",
  version: 1,
  nodes: [
    {
      input_schemas: {},
      source: "",
      key: "queue",
      id: "queue",
      kind: "param",
      signature: "string",
      detail: "Name the queue this run should sweep.",
      output_schema: { type: "string" },
      required: true,
    },
    {
      input_schemas: {},
      source: "",
      key: "compose",
      id: "compose",
      kind: "run",
      signature: "",
      detail: "",
      output_schema: null,
      is_output: true,
    },
  ],
  edges: [{ from: "queue", to: "compose", kind: "wire" }],
};

const ARTIFACT: Wire<typeof ArtifactRefSchema> = {
  id: "write-up",
  label: "The weekly write up",
  run_id: "run-1",
  node_id: "compose",
  turn: null,
  value_digest: "d".repeat(64),
  media_type: "text/markdown",
};

const READY = { executable: true, described: true, findings: [] };

const LIMITS: Wire<typeof RunLimitsSchema> = {
  brakes: [
    { window: "run", usd: "2", warn_at: null },
    { window: "day", usd: "25", warn_at: 0.8 },
  ],
  gate_modes: ["wait", "orchestrate", "auto"],
  default_gate_mode: "wait",
};

const DETAIL: Wire<typeof WorkflowDetailSchema> = {
  archived_at: null,
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
    approved_by: [],
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
    inputs: [{ key: "queue", label: "What to sweep", control: "text" }],
    result_fields: [],
    actions: [],
  },
};

const DRAFTED = {
  spec_yaml: "name: wf.weekly-sweep\nversion: 1\n",
  title: "Weekly sweep",
  summary: "Checks the queue every week and writes up what changed.",
  applicability: "Use at the start of a week.",
  lineage: null,
  presentation: DETAIL.presentation,
  intent: {
    request: REQUEST,
    expected_result: "A short list of what changed, and nothing else.",
    success_example: "",
    empty_input_behavior: "",
    failure_behavior: "",
    trigger: "scheduled",
    assumptions: [ASSUMPTION],
  },
};

const ADAPTED_DESCRIPTION = "Sweeps the open queue, and says when the queue was empty.";

const CHILD_DRAFT = {
  ...DRAFTED,
  title: "Weekly sweep",
  lineage: {
    parent_name: "wf.weekly-sweep",
    parent_version: 1,
    touched: [],
    ops: [{ op: "set_description", to: ADAPTED_DESCRIPTION }],
    evidence: [
      "Adapted from run run-1 of wf.weekly-sweep.v1.",
      "Observed outcome: partial. One of the two sections came back empty.",
      "What the owner asked for: it never said the second section was empty.",
    ].join("\n"),
  },
};

function endedRun(): Wire<typeof RunEnvelopeSchema> {
  return {
    spend: null,
    run_id: "run-1",
    workflow_id: WORKFLOW_ID,
    run_title: RUN_TITLE,
    outcome: "completed",
    failure: "",
    started_at: "2026-09-16T09:00:00Z",
    ended_at: "2026-09-16T09:06:00Z",
    runner: "app:harness",
    session_id: RUN_SESSION,
    params: { queue: "the open queue" },
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    result: {
      status: "partial",
      summary: "One of the two sections came back empty.",
      attempted: 2,
      succeeded: 1,
      skipped: 0,
      failed: 1,
      retry_attempts: 0,
      primary_input: 2,
      generation: 0,
      checks: [],
      artifacts: [ARTIFACT],
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

interface Journey {
  /** True once the design turn has settled, which is when the proposal exists. */
  drafted: boolean;
  saved: boolean;
  runs: Wire<typeof RunViewSchema>[];
  recent: Wire<typeof RunEnvelopeSchema>[];
  /** Every run start that reached the backend, as posted. */
  launched: Record<string, unknown>[];
  /** Every session rename that reached the backend, as posted. */
  renamed: Record<string, unknown>[];
  /** Every adaptation that reached the backend, as posted. */
  adapted: Record<string, unknown>[];
  /** Every schedule write that reached the backend. Designing must leave this empty. */
  scheduled: Record<string, unknown>[];
}

function freshJourney(): Journey {
  return {
    drafted: false,
    saved: false,
    runs: [],
    recent: [],
    launched: [],
    renamed: [],
    adapted: [],
    scheduled: [],
  };
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
    turns: [
      { who: "you", text: REQUEST },
      { who: "agent", text: "Here is the shape it would run. Read it and tell me what to change." },
    ],
    draft: DRAFTED,
    issues: [],
    valid: true,
    graph: GRAPH,
    readiness: READY,
    intent_issues: ["success_example", "empty_input_behavior", "failure_behavior"],
  };
}

// One router for every request the walk can make, so nothing can reach a real backend.
async function mockApi(page: Page, journey: Journey): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
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
      // Nothing is in flight here, which is what the steer endpoint says with a 409 and what
      // sends the message as an ordinary turn instead.
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "no turn is in flight" }),
      });
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
    if (path === "/api/runs") return json(route, { runs: journey.runs });
    if (path === "/api/runs/recent") return json(route, { runs: journey.recent });
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
        subject: "run-1",
        since_hours: 24,
        reason: "no telemetry here",
        fallback: "the run's own ledger",
      });
    }
    if (path.startsWith("/api/workflow/")) return json(route, DETAIL);
    if (path.endsWith("/history") && path.startsWith("/api/workflows/")) {
      return json(route, { runs: journey.recent });
    }
    if (path.endsWith("/artifacts/resolve")) {
      return json(route, { ref: ARTIFACT, value: "The queue moved twice this week." });
    }
    if (path.startsWith("/api/runs/history/")) {
      return json(route, {
        ...endedRun(),
        spec: {},
        values: {},
        conversations: {},
        events: [],
        graph: GRAPH,
      });
    }
    if (path.endsWith("/conversation")) {
      const id = path.split("/")[3] ?? "";
      return json(route, conversation(journey, id));
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
      journey.launched.push((await request.postDataJSON()) as Record<string, unknown>);
      return json(route, { ok: true, running: WORKFLOW_ID });
    }
    if (path === "/api/sessions" && method === "POST") {
      const asked = (await request.postDataJSON()) as { kind?: string };
      const id = asked.kind === "runner" ? RUN_SESSION : ADAPT_SESSION;
      return json(route, { session: session(id, "", asked.kind ?? "chat") });
    }
    if (path === "/api/sessions") return json(route, { sessions: [] });
    if (path.startsWith("/api/sessions/") && method === "PATCH") {
      journey.renamed.push((await request.postDataJSON()) as Record<string, unknown>);
      return json(route, { session: session(path.split("/")[3] ?? "", "renamed") });
    }
    if (path === `/api/sessions/${DESIGN_SESSION}`) {
      return json(route, { session: session(DESIGN_SESSION, DESIGN_SESSION_TITLE), turns: [] });
    }
    if (path === "/api/sessions/m-1") {
      return json(route, { session: session("m-1", "master", "master"), turns: [] });
    }
    return json(route, {});
  });
}

/** A route this walk crosses for the first time is compiled on demand, so crossing one waits longer. */
const CROSSING = { timeout: 30_000 };

test("goes from a plain request to a proposal, a run, its artifact, a rerun, and the next version", async ({
  page,
}) => {
  const journey = freshJourney();
  await mockApi(page, journey);

  // 1. Ask for the automation in ordinary words. Designing designs, and reaches nothing else.
  await page.goto(`/workflows/draft?session=${DESIGN_SESSION}`);
  await expect(
    page.getByRole("heading", { name: "What should this automation do?" }),
  ).toBeVisible(CROSSING);
  await page.getByLabel("What should this automation do?").fill(REQUEST);
  await page.getByRole("button", { name: "Design it" }).click();

  // 2. The proposal, the request in the person's own words, and the assumption made for them.
  await expect(page.getByRole("heading", { name: "What you asked for" })).toBeVisible(CROSSING);
  await expect(page.getByText(REQUEST).first()).toBeVisible();
  await expect(page.locator(`input[value="${ASSUMPTION}"]`)).toBeVisible();
  await expect(page.getByText("Inferred")).toBeVisible();
  expect(journey.launched).toHaveLength(0);
  expect(journey.scheduled).toHaveLength(0);

  // 3. Saving keeps it in the library and still starts nothing.
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(`Saved as ${WORKFLOW_ID}`)).toBeVisible();
  expect(journey.saved).toBe(true);
  expect(journey.launched).toHaveLength(0);
  expect(journey.scheduled).toHaveLength(0);

  // 4. Its own page: the typed input, what the run is held to, and one deliberate start.
  await page.goto(`/workflows/${WORKFLOW_ID}`);
  await page.getByRole("button", { name: "Run", exact: true }).click(CROSSING);
  const setup = page.getByRole("dialog");
  await expect(setup).toBeVisible();
  await expect(setup.getByText("$2 per run")).toBeVisible();
  await expect(setup.getByText("$25 per day, warned at 80%")).toBeVisible();
  await setup.getByLabel("What to sweep").fill("the open queue");
  await setup.getByRole("button", { name: "Run it" }).click();

  await expect.poll(() => journey.launched.length).toBe(1);
  expect(journey.launched[0]).toEqual({
    workflow_id: WORKFLOW_ID,
    arguments: { queue: "the open queue" },
    run_title: RUN_TITLE,
  });
  // The run and the session holding it are named by the workflow and this setup. The session the
  // designing happened in lent its name to neither.
  expect(journey.renamed).toEqual([{ title: "Weekly sweep" }]);
  expect(JSON.stringify(journey.launched)).not.toContain(DESIGN_SESSION_TITLE);

  // 5. What the run actually left behind, read through the run's own route.
  journey.recent = [endedRun()];
  await page.goto(`/workflows/${WORKFLOW_ID}?run=run-1`);
  await expect(page.getByText("What this run left behind")).toBeVisible(CROSSING);
  await expect(page.getByText("The weekly write up")).toBeVisible();
  await page.getByRole("button", { name: "Show" }).click();
  await expect(page.getByText("The queue moved twice this week.").first()).toBeVisible();

  // 6. Run it again on what that run was given, with one value changed. Still a deliberate start.
  await page.getByRole("button", { name: "Run again" }).click();
  const again = page.getByRole("dialog");
  await expect(again.getByLabel("What to sweep")).toHaveValue("the open queue");
  await again.getByLabel("What to sweep").fill("the closed queue");
  await again.getByRole("button", { name: "Run it" }).click();

  await expect.poll(() => journey.launched.length).toBe(2);
  expect(journey.launched[1]).toMatchObject({ arguments: { queue: "the closed queue" } });
  expect(journey.launched[0]).toMatchObject({ arguments: { queue: "the open queue" } });

  // 7. The next version, made out of the run that disappointed. The one it came from is unchanged.
  await page.getByRole("button", { name: "Make a new version" }).click();
  const adapting = page.getByRole("dialog");
  await expect(adapting.getByText("Adapting from Weekly sweep, the open queue")).toBeVisible();
  await adapting.getByLabel("What this version does").fill(ADAPTED_DESCRIPTION);
  await adapting
    .getByLabel("Why")
    .fill("it never said the second section was empty");
  await adapting.getByRole("button", { name: "Make the new version" }).click();

  await expect.poll(() => journey.adapted.length).toBe(1);
  expect(journey.adapted[0]).toEqual({
    workflow_id: WORKFLOW_ID,
    ops: [{ op: "set_description", to: ADAPTED_DESCRIPTION }],
    source_run_id: "run-1",
    feedback: "it never said the second section was empty",
  });

  await expect(page).toHaveURL(new RegExp(`session=${ADAPT_SESSION}`), CROSSING);
  const lineage = page.locator('[data-section="adapted-from"]');
  await expect(lineage).toContainText("A new version of wf.weekly-sweep v1");
  await expect(lineage).toContainText("The one it came from is unchanged.");
  await expect(lineage).toContainText("Adapted from run run-1 of wf.weekly-sweep.v1.");
});
