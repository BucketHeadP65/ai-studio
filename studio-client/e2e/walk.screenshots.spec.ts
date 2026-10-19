import { type Page, type Route, expect, test } from "@playwright/test";

import type { z } from "zod";

import type { KnowledgeBaseDetailSchema, KnowledgeBaseListResultSchema } from "@/api/types";
import type { RunListSchema, SessionListSchema } from "@/api/sessions-types";
import type { LibraryEntrySchema, RunEnvelopeSchema } from "@/api/studio-types";

// Every mock below is annotated with the INPUT type of the schema the app parses it with, so a
// fixture that drifts from the contract fails the typecheck rather than the page. The input type
// rather than the output one, because a mock is what the wire sends: a field the schema defaults
// is optional here, exactly as it is over HTTP.
type Wire<S extends { _zod: { input: unknown } }> = z.input<S>;

// The design gate's screenshot walk: every station, populated with plausible
// fixtures, captured in the light and one dark theme. The PNGs land under
// test-results/walk/ for the merge review's visual pass. Assertion-free on
// purpose — the smoke spec owns the UI contract; this spec exists to be looked at.

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const PLUGINS = {
  plugins: [
    {
      name: "research",
      description: "The knowledge bases' read plugin, search and the leveled fetch.",
      enabled: true,
      fixed: false,
    },
    {
      name: "leveled-reads",
      description: "The leveled-read governor, what a descent may cost in context.",
      enabled: true,
      fixed: false,
    },
    {
      name: "sql",
      description: "The SQL plugin over the same knowledge bases, run_sql and describe_schema.",
      enabled: false,
      fixed: false,
    },
    {
      name: "pydantic-ai-docs",
      description: "The current pydantic-ai docs, one tool away.",
      enabled: true,
      fixed: false,
    },
    {
      name: "recall",
      description: "Search over past conversations and runs.",
      enabled: true,
      fixed: false,
    },
    {
      name: "skills",
      description: "The agent's skill files, loaded from the skills directory.",
      enabled: true,
      fixed: false,
    },
    { name: "shell", description: "Run commands in the checkout.", enabled: true, fixed: false },
    {
      name: "files",
      description: "The checkout as files, reads riding code mode and writes as acts.",
      enabled: true,
      fixed: false,
    },
    {
      name: "author-capabilities",
      description: "The capability-authoring plugin, write_custom_capability.",
      enabled: true,
      fixed: false,
    },
    {
      name: "author-scripts",
      description: "The script-authoring plugin, write_custom_step.",
      enabled: true,
      fixed: false,
    },
    {
      name: "author-agents",
      description: "The agent-authoring plugin, write_custom_agent.",
      enabled: true,
      fixed: false,
    },
    {
      name: "author-profiles",
      description: "The profile-authoring plugin, write_custom_profile.",
      enabled: true,
      fixed: false,
    },
    {
      name: "workflows",
      description: "The workflow process, the library's verbs, the validator, and the runner.",
      enabled: true,
      fixed: true,
    },
    {
      name: "spend-limits",
      description: "The money brake, per run and per day.",
      enabled: true,
      fixed: true,
    },
    {
      name: "code-mode",
      description: "Look-and-compute tools folded into one program per request.",
      enabled: true,
      fixed: true,
    },
    {
      name: "loop-detection",
      description: "The nudge when the agent circles.",
      enabled: true,
      fixed: true,
    },
    {
      name: "output-limits",
      description: "Oversized tool output spilled with a read-back handle, never flooded.",
      enabled: true,
      fixed: true,
    },
    {
      name: "step-persistence",
      description: "Every turn recorded, so the conversation survives a restart.",
      enabled: true,
      fixed: true,
    },
    {
      name: "instructions",
      description: "The agent's standing instructions.",
      enabled: true,
      fixed: true,
    },
  ],
};

const ARTIFACTS: Record<string, unknown[]> = {
  capability: [
    {
      name: "summarize_day",
      status: "active",
      descriptor: "SummarizeDay",
      last_error: null,
      promoted: true,
      next_step: null,
    },
    {
      name: "knowledge_base_pulse",
      status: "disabled",
      descriptor: "KnowledgeBasePulse",
      last_error: null,
      promoted: false,
      next_step: "activate",
    },
  ],
  step: [
    {
      name: "fetch_prior_art",
      status: "disabled",
      descriptor: "fetch_prior_art",
      last_error: null,
      promoted: null,
      next_step: "activate",
    },
  ],
  profile: [
    {
      name: "researcher",
      status: "active",
      descriptor: "plugins: research, leveled-reads, sql",
      last_error: null,
      promoted: null,
      next_step: null,
    },
  ],
  agent: [
    {
      name: "ledger_weigher",
      status: "disabled",
      descriptor: "(host default model)",
      last_error: "capability 'nonexistent' is not a known capability name",
      promoted: null,
      next_step: null,
    },
  ],
};

const LIBRARY: Wire<typeof LibraryEntrySchema>[] = [
  {
    archived_at: null,
    shape: { steps: 0, person_gates: 0, agent_gates: 0 },
    runs: { last: null },
    id: "wf.lit-check.v5",
    title: "Literature check into a decision record",
    summary: "Search the knowledge base for prior art, rank the risks, write a dated record.",
    provisional: false,
    score: 3,
    trust: {
      approved_by: [],
      level: "machine-confirmed",
      runs_with_evidence: 2,
      failed_runs: 0,
      structural_grades: 6,
      structural_mean: 0.65,
      structural_last: 0.7,
    },
    needs: [],
    missing: [],
    brings: ["find_prior_art"],
    version: 5,
  },
  {
    archived_at: null,
    shape: { steps: 0, person_gates: 0, agent_gates: 0 },
    runs: { last: null },
    id: "wf.metric-sweep.v1",
    title: "Metric sweep",
    summary: "Walk the eval metrics and flag drift against the last blessed run.",
    provisional: false,
    score: 1,
    trust: {
      approved_by: [],
      level: "unverified",
      runs_with_evidence: 0,
      failed_runs: 0,
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    needs: ["fetch_sales"],
    missing: ["fetch_sales"],
    brings: [],
    version: 1,
  },
];

const STEPS = [
  {
    name: "find_prior_art",
    description: "Search the knowledge base for prior art.",
    signature: "find_prior_art(context: string) -> PriorArtFindings",
    output: "PriorArtFindings",
    tools: ["search"],
  },
  {
    name: "write_decision_record",
    description: "Write the dated record.",
    signature: "write_decision_record(findings: PriorArtFindings) -> str",
    output: "str",
    tools: [],
  },
];

const CONVERSATION = {
  session_id: "s-1",
  turns: [
    { who: "you", text: "draft a workflow that checks a decision against the literature" },
    {
      who: "agent",
      text: "Drafted **lit-check** with six steps. The draft validates, save it when ready.",
    },
  ],
};

function usage() {
  return { input_tokens: 1200, output_tokens: 400, cache_read_tokens: 0, cache_write_tokens: 0 };
}

const PROJECTS = [{ id: "proj-1", name: "Engram", root_path: "/Users/owner/engram" }];

// The fleet board's own fixtures: the live pool (`/api/runs`) for the
// waiting/running/paused lanes, the archive (`/api/runs/recent`) for Recent.
const FLEET_RUNS: Wire<typeof RunListSchema>["runs"] = [
  {
    run_title: "",
    started_at: "",
    session_id: "s3",
    workflow_id: "wf.lit-check.v5",
    state: "waiting",
    active: [],
    done: ["intake", "search"],
    failed: [],
    waiting_at: "review",
    gate: {
      gate_id: "review",
      workflow: "wf.lit-check.v5",
      reviewing: '{"title": "a draft, ready for review"}',
      rejection_reruns: "compose",
    },
  },
  {
    run_title: "",
    started_at: "",
    session_id: "s4",
    workflow_id: "wf.metric-sweep.v1",
    state: "running",
    active: ["fetch_sales"],
    done: ["load_metrics"],
    failed: [],
    waiting_at: null,
    gate: null,
  },
  {
    run_title: "",
    started_at: "",
    session_id: "s5",
    workflow_id: "wf.lit-check.v5",
    state: "paused",
    active: [],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    gate: null,
  },
];

const RECENT_RUNS: Wire<typeof RunEnvelopeSchema>[] = [
  {
    run_title: "",
    gates_orchestrated: [],
    spend: null,
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-20260820091200-a1b2",
    workflow_id: "wf.lit-check.v5",
    outcome: "completed",
    failure: "",
    started_at: "2026-08-20T09:00:00Z",
    ended_at: "2026-08-20T09:12:00Z",
    runner: "app:harness",
    session_id: "s1",
    params: {},
    gates_approved: ["review"],
    gates_rejected: {},
    gates_auto: [],
    pinned: true,
  },
  {
    run_title: "",
    gates_orchestrated: [],
    spend: null,
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-20260819090300-c3d4",
    workflow_id: "wf.metric-sweep.v1",
    outcome: "failed",
    failure: "fetch_sales timed out",
    started_at: "2026-08-19T09:00:00Z",
    ended_at: "2026-08-19T09:03:00Z",
    runner: "app:schedule:nightly",
    session_id: "s2",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    pinned: false,
  },
];

const SESSIONS: Wire<typeof SessionListSchema>["sessions"] = [
  {
    id: "s1",
    project_id: "proj-1",
    title: "Literature check into a decision record",
    started_at: "2026-08-20T09:00:00Z",
    last_activity_at: "2026-08-20T09:20:00Z",
    archived: false,
    answering: false,
    turn_count: 6,
    usage: usage(),
    kind: "chat",
    cost_usd: "0.0421",
    context: null,
  },
  {
    id: "s2",
    project_id: "proj-1",
    title: "Metric sweep against last blessed run",
    started_at: "2026-08-19T09:00:00Z",
    last_activity_at: "2026-08-19T09:05:00Z",
    archived: false,
    answering: false,
    turn_count: 2,
    usage: usage(),
    kind: "runner",
    cost_usd: "0.0083",
    context: null,
  },
];

const KNOWLEDGE_BASES: Wire<typeof KnowledgeBaseListResultSchema> = {
  knowledge_bases: [
    {
      name: "research",
      registered: true,
      indexed: true,
      broken: false,
      path: "/Users/owner/knowledge-bases/research",
      description: "the retrieval literature",
      pages: 143,
      page_types: ["paper", "concept"],
      last_indexed: "2026-08-20T09:00:00+00:00",
    },
    {
      name: "inbox",
      registered: true,
      indexed: false,
      broken: false,
      path: "/Users/owner/knowledge-bases/inbox",
      description: "",
      pages: 0,
      page_types: [],
      last_indexed: null,
    },
  ],
};

// The knowledge base detail the walk opens for its Graph-tab capture: the listing row
// plus the detail-only fields the page reads.
const KNOWLEDGE_BASE_DETAIL: Wire<typeof KnowledgeBaseDetailSchema> = {
  ...KNOWLEDGE_BASES.knowledge_bases[0],
  sources_connectors: [],
  watch_list: [],
  validation: [],
  source_queue: [],
  last_refresh_run: null,
  schedule: null,
};

async function mockApi(page: Page) {
  // The dashboard opens one EventSource per backend on mount. Answered with a stream that says
  // nothing and asks for a long retry, so no spec spends its budget on reconnects.
  await page.route("**/api/events**", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "retry: 100000\n\n" }),
  );
  await page.route("**/api/graph**", (route) =>
    json(route, {
      nodes: [
        {
          id: "note:budget",
          type: "note",
          title: "The budget speaks",
          status: "current",
          superseded: false,
        },
        { id: "paper:rag", type: "paper", title: "RAG", status: "current", superseded: false },
      ],
      edges: [{ src_id: "note:budget", dst_id: "paper:rag", rel: "cites", provenance_claim: null }],
    }),
  );
  await page.route("**/api/knowledge-bases", (route) => json(route, KNOWLEDGE_BASES));
  await page.route("**/api/knowledge-bases/research", (route) => json(route, KNOWLEDGE_BASE_DETAIL));
  await page.route("**/api/knowledge-bases/research/refresh-runs", (route) => json(route, { refresh_runs: [] }));
  await page.route("**/api/knowledge-bases/research/decisions", (route) =>
    json(route, { rows: [], skipped: [], outcomes_by_page_type: {} }),
  );
  await page.route("**/api/library", (route) => json(route, LIBRARY));
  await page.route("**/api/steps", (route) => json(route, STEPS));
  await page.route("**/api/conversation", (route) => json(route, CONVERSATION));
  await page.route("**/api/sessions**", (route) => json(route, { sessions: SESSIONS }));
  // One route covers the live pool and the archive alike: the fleet page fires
  // both `/api/runs` and `/api/runs/recent` on mount, told apart by pathname.
  await page.route("**/api/runs**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/runs/recent") {
      return json(route, { runs: RECENT_RUNS });
    }
    return json(route, { runs: FLEET_RUNS });
  });
  await page.route("**/api/projects", (route) => json(route, { projects: PROJECTS }));
  await page.route("**/api/run/state**", (route) =>
    json(route, {
      auto_approve: false,
      gate_mode: "wait",
      running: false,
      workflow: null,
      result: null,
      active: [],
      done: [],
      failed: [],
      waiting_at: null,
      events: [],
      seq: 0,
    }),
  );
  await page.route("**/api/pending", (route) => json(route, { pending: null }));
  await page.route("**/api/harness/*", (route) => {
    const kind = new URL(route.request().url()).pathname.split("/").at(-1) ?? "";
    return json(route, { kind, artifacts: ARTIFACTS[kind] ?? [] });
  });
  await page.route("**/api/harness/composition", (route) =>
    json(route, {
      model: "gpt-5.4",
      run_engine: "in_process",
      steps_registered: ["find_prior_art", "write_decision_record"],
      promoted: ["summarize_day"],
      active: { capability: 1, step: 0, agent: 0, profile: 1 },
    }),
  );
  await page.route("**/api/harness/model", (route) =>
    json(route, { model: "gpt-5.4", choices: ["gpt-5.4", "gpt-5.4-mini"], selected: null }),
  );
  await page.route("**/api/harness/plugins", (route) => json(route, PLUGINS));
  await page.route("**/api/harness/selection", (route) => json(route, { name: null }));
}

// The stations scroll inside the layout's own main, so a fullPage capture stops at
// the window: a tall viewport shows the whole station instead.
test.use({ viewport: { width: 1440, height: 2200 } });

for (const theme of ["paper", "graphite"] as const) {
  test(`walk and capture every station (${theme})`, async ({ page }) => {
    await mockApi(page);
    await page.addInitScript(
      (chosen) => window.localStorage.setItem("studio-theme", chosen),
      theme,
    );

    for (const [path, name] of [
      ["/", "home"],
      ["/chat", "chat"],
      ["/workflows", "fleet"],
      ["/knowledge-bases", "knowledge-bases"],
      ["/harness", "harness"],
    ] as const) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      await page.screenshot({ path: `test-results/walk/${name}-${theme}.png`, fullPage: true });
    }

    // The graph lives inside a knowledge base's own page now: capture its tab too,
    // after the canvas has real nodes rather than the loading text.
    await page.goto("/knowledge-bases/research");
    await page.waitForLoadState("networkidle");
    await page.getByRole("tab", { name: "Graph" }).click();
    await expect(page.getByText("2 pages, 1 links")).toBeVisible();
    await expect(page.getByText("Loading the graph.")).toBeHidden();
    await page.screenshot({
      path: `test-results/walk/knowledge-base-graph-${theme}.png`,
      fullPage: true,
    });
  });
}
