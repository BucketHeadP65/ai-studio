import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { server } from "@/test-support/msw/server";

import { refusedReadiness, studioApi, studioErrorMessage } from "./studio";

// The studio talks to a DIFFERENT backend than the rest of the dashboard — the
// studio agent backend on :8770, faked here as http://studio.test (vitest.config env).
// Every test pins one endpoint's zod boundary with a payload mirroring the
// FastAPI routes' actual JSON, so a backend contract drift fails HERE, named,
// rather than as an undefined deep in a canvas render.

const BASE = "http://studio.test";

const CARD = {
  archived_at: null,
  shape: { steps: 0, person_gates: 0, agent_gates: 0 },
  runs: { last: null },
  id: "wf.lit-check.v5",
  title: "Literature check → decision record",
  summary: "Search the knowledge base for prior art, rank the risks, write a dated ADR.",
  provisional: false,
  score: 3.0,
  trust: { structural_grades: 0, structural_mean: null, structural_last: null, approved_by: [], level: "machine-confirmed", runs_with_evidence: 2, failed_runs: 0 },
  needs: [],
  missing: [],
  brings: ["find_prior_art", "rank_risks"],
  version: 5,
};

const GRAPH = {
  name: "lit-check",
  version: 5,
  nodes: [
    {
      output_schema: null,
      input_schemas: {},
      source: "",
      key: "param:context",
      id: "context",
      kind: "param",
      signature: "string",
      detail: "The context the decision applies to",
      tools: [],
      is_output: false,
      unknown: false,
      brought: false,
      briefing: "",
    },
    {
      output_schema: null,
      input_schemas: {},
      source: "",
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "compose_decision_record(context: string) → DecisionRecord",
      detail: "Write the record.",
      tools: ["run_sql"],
      is_output: true,
      unknown: false,
      brought: true,
      briefing: "Compose a dated ADR.",
    },
  ],
  edges: [{ from: "param:context", to: "draft", kind: "wire", label: "context: string" }],
};

const WORKFLOW = {
  title: "",
  summary: "",
  applicability: "",
  archived_at: null,
  ok: true,
  id: "wf.lit-check.v5",
  name: "lit-check",
  version: 5,
  description: "Check a decision against the literature.",
  provisional: false,
  graph: GRAPH,
  issues: [],
  fits: true,
  missing: [],
  brings: ["compose_decision_record"],
  spec_yaml: "name: lit-check\nversion: 5\n",
  staleness: [],
  lineage: {
    parent_name: "lit-check",
    parent_version: 4,
    touched: ["draft"],
    ops: [{ op: "set_description", value: "x" }],
    evidence: "the judge gate rejected briefs it should have passed",
    parent_id: "wf.lit-check.v4",
  },
  birth: null,
  carried: ["counter", "per_risk"],
  trust: {
    approved_by: [],
    level: "machine-confirmed",
    runs_with_evidence: 2,
    failed_runs: 0,
    structural_grades: 3,
    structural_mean: 0.75,
    structural_last: 0.9,
  },
  predictions: [
    {
      claim: "produces",
      subject: "draft",
      shape: "Record",
      holds: true,
      observed: "2 completed run(s) produced Record",
      reads: "HOLDS — draft produces Record: 2 completed run(s) produced Record",
    },
    {
      claim: "completes",
      subject: "",
      shape: "",
      holds: null,
      observed: "no completed run of this version yet",
      reads: "UNMEASURABLE — a run of this version completes: no completed run of this version yet",
    },
  ],
  prediction_record: { hits: 1, misses: 0, unmeasurable: 1, hit_rate: 1 },
};

const RUN_ENVELOPE = {
  spend: null,
  result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
  run_id: "run-1",
  workflow_id: "wf.lit-check.v5",
  run_title: "",
  outcome: "completed",
  failure: "",
  started_at: "2026-08-23T10:00:00Z",
  ended_at: "2026-08-23T10:05:00Z",
  runner: "chat",
  session_id: "sess-1",
  params: { context: "a payments system" },
  gates_approved: ["review"],
  gates_rejected: {},
  gates_auto: [],
  gates_orchestrated: [],
  pinned: false,
};

const RUN_RECORD = {
  graph: null,
  conversations: {},
  owner_reviews: [],
  ...RUN_ENVELOPE,
  spec: { name: "lit-check" },
  values: { draft: "the record text" },
  events: [{ seq: 1, node: "draft", at: 1.5, finished: true, failed: false }],
};

describe("studioApi", () => {
  it("parses the library, trust and fit included", async () => {
    server.use(http.get(`${BASE}/api/library`, () => HttpResponse.json([CARD])));
    const cards = await studioApi.library();
    expect(cards).toHaveLength(1);
    expect(cards[0]?.trust.level).toBe("machine-confirmed");
    expect(cards[0]?.version).toBe(5);
  });

  it("parses one workflow with its graph and trust facts", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${encodeURIComponent("wf.lit-check.v5")}`, () =>
        HttpResponse.json(WORKFLOW),
      ),
    );
    const detail = await studioApi.workflow("wf.lit-check.v5");
    expect(detail.graph.nodes).toHaveLength(2);
    expect(detail.staleness).toEqual([]);
    expect(detail.lineage?.parent_id).toBe("wf.lit-check.v4");
    expect(detail.carried).toEqual(["counter", "per_risk"]);
    // A claim no run could answer crosses the boundary as `null`, not as `false`:
    // the schema types the tri-state, so nothing downstream can read it as a miss.
    expect(detail.predictions?.map((claim) => claim.holds)).toEqual([true, null]);
    expect(detail.prediction_record?.hit_rate).toBe(1);
    expect(detail.lineage?.evidence).toContain("rejected briefs");
    // The trust facts this test's own name promises — earned level plus what was graded,
    // reaching the artifact view the same way they already reach the library card.
    expect(detail.trust.level).toBe("machine-confirmed");
    expect(detail.trust.structural_grades).toBe(3);
    expect(detail.trust.structural_mean).toBe(0.75);
  });

  it("parses a graph carrying an orchestrator gate node, the kind that used to break the whole page", async () => {
    const id = "wf.orchestrated.v1";
    server.use(
      http.get(`${BASE}/api/workflow/${encodeURIComponent(id)}`, () =>
        HttpResponse.json({
          ...WORKFLOW,
          id,
          graph: {
            ...GRAPH,
            nodes: [
              ...GRAPH.nodes,
              {
                output_schema: null,
                input_schemas: {},
                source: "",
                key: "review",
                id: "review",
                kind: "orchestrator-gate",
                signature: "this run's orchestrator decides, DecisionRecord passes through",
                detail: "The orchestrator decides, not a person, against the charge below.",
                tools: [],
                is_output: false,
                unknown: false,
                brought: false,
                briefing: "Check the record cites at least one prior decision before it clears.",
              },
            ],
            edges: [
              ...GRAPH.edges,
              { from: "draft", to: "review", kind: "wire", label: "reviews: DecisionRecord" },
              { from: "review", to: "draft", kind: "rejected", label: "rejected" },
            ],
          },
        }),
      ),
    );

    const detail = await studioApi.workflow(id);

    const gate = detail.graph.nodes.find((node) => node.id === "review");
    expect(gate?.kind).toBe("orchestrator-gate");
    expect(gate?.briefing).toContain("prior decision");
  });

  it("parses an artifact that claimed nothing as claiming nothing, not as claiming zero", async () => {
    const id = "wf.plain.v1";
    server.use(
      http.get(`${BASE}/api/workflow/${encodeURIComponent(id)}`, () =>
        HttpResponse.json({
          ...WORKFLOW,
          id,
          lineage: null,
          predictions: null,
          prediction_record: null,
        }),
      ),
    );
    const detail = await studioApi.workflow(id);
    expect(detail.predictions).toBeNull();
    expect(detail.prediction_record).toBeNull();
  });

  it("reads the conversation as turns", async () => {
    server.use(
      http.get(`${BASE}/api/conversation`, () =>
        HttpResponse.json({ turns: [{ who: "you", text: "hi" }], session_id: "s-1" }),
      ),
    );
    const { turns } = await studioApi.conversation();
    expect(turns[0]).toEqual({ who: "you", text: "hi" });
  });

  it("polls run state with a since-cursor in the query", async () => {
    server.use(
      http.get(`${BASE}/api/run/state`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("since")).toBe("3");
        return HttpResponse.json({
          auto_approve: false,
          gate_mode: "wait",
          running: true,
          workflow: "wf.lit-check.v5",
          result: null,
          active: ["draft"],
          done: ["counter"],
          failed: [],
          waiting_at: null,
          events: [{ seq: 4, node: "draft", at: 1.25, finished: false, failed: false }],
          seq: 4,
        });
      }),
    );
    const state = await studioApi.runState(3);
    expect(state.events[0]?.node).toBe("draft");
    expect(state.seq).toBe(4);
  });

  it("reads a waiting gate and answers it", async () => {
    server.use(
      http.get(`${BASE}/api/pending`, () =>
        HttpResponse.json({
          pending: {
            gate_id: "gate",
            workflow: "wf.lit-check.v5",
            reviewing: "{...}",
            rejection_reruns: "compose",
          },
        }),
      ),
      http.post(`${BASE}/api/decide`, () => HttpResponse.json({ ok: true, approved: true })),
    );
    const { pending } = await studioApi.pending();
    expect(pending?.gate_id).toBe("gate");
    const decided = await studioApi.decide(true);
    expect(decided.approved).toBe(true);
  });

  it("posts a rejection with feedback, and omits it when none is given", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/decide`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, approved: false });
      }),
    );

    await studioApi.decide(false, "too risky");
    expect(posted).toEqual({ approve: false, feedback: "too risky" });

    await studioApi.decide(true);
    expect(posted).toEqual({ approve: true });
  });

  it("flips automode on the live run", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/run/automode`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, auto_approve: true });
      }),
    );

    const result = await studioApi.setAutomode(true);

    expect(posted).toEqual({ on: true });
    expect(result).toEqual({ ok: true, auto_approve: true });
  });

  it("surfaces a refused save as its detail, not as JSON soup", async () => {
    server.use(
      http.post(`${BASE}/api/save`, () =>
        HttpResponse.json({ ok: false, detail: "the draft does not validate" }, { status: 400 }),
      ),
    );
    const failure = await studioApi.save().catch((error: unknown) => studioErrorMessage(error));
    expect(failure).toBe("the draft does not validate");
  });

  it("reads one workflow's ended-run history, the limit riding the query string", async () => {
    server.use(
      http.get(
        `${BASE}/api/workflows/${encodeURIComponent("wf.lit-check.v5")}/history`,
        ({ request }) => {
          expect(new URL(request.url).searchParams.get("limit")).toBe("10");
          return HttpResponse.json({ runs: [RUN_ENVELOPE] });
        },
      ),
    );

    const runs = await studioApi.workflowHistory("wf.lit-check.v5", 10);

    expect(runs).toHaveLength(1);
    expect(runs[0]?.run_id).toBe("run-1");
    expect(runs[0]?.outcome).toBe("completed");
  });

  it("rejects a history envelope with an outcome outside the enum", async () => {
    server.use(
      http.get(`${BASE}/api/workflows/${encodeURIComponent("wf.lit-check.v5")}/history`, () =>
        HttpResponse.json({ runs: [{ ...RUN_ENVELOPE, outcome: "aborted" }] }),
      ),
    );

    await expect(studioApi.workflowHistory("wf.lit-check.v5")).rejects.toThrow(ZodError);
  });

  it("reads ended runs across every workflow", async () => {
    server.use(
      http.get(`${BASE}/api/runs/recent`, ({ request }) => {
        expect(new URL(request.url).searchParams.get("limit")).toBe("20");
        return HttpResponse.json({ runs: [RUN_ENVELOPE] });
      }),
    );

    const runs = await studioApi.recentRuns(20);

    expect(runs).toEqual([RUN_ENVELOPE]);
  });

  it("rejects a recent-runs envelope with a wrong-typed pinned field", async () => {
    server.use(
      http.get(`${BASE}/api/runs/recent`, () =>
        HttpResponse.json({ runs: [{ ...RUN_ENVELOPE, pinned: "yes" }] }),
      ),
    );

    await expect(studioApi.recentRuns()).rejects.toThrow(ZodError);
  });

  it("reads one archived run's full record, spec, values, and events included", async () => {
    server.use(http.get(`${BASE}/api/runs/history/run-1`, () => HttpResponse.json(RUN_RECORD)));

    const record = await studioApi.runRecord("run-1");

    expect(record.values.draft).toBe("the record text");
    expect(record.events[0]?.node).toBe("draft");
    expect(record.spec).toEqual({ name: "lit-check" });
  });

  it("rejects a run record whose event carries a wrong-typed seq", async () => {
    server.use(
      http.get(`${BASE}/api/runs/history/run-1`, () =>
        HttpResponse.json({ ...RUN_RECORD, events: [{ ...RUN_RECORD.events[0], seq: "one" }] }),
      ),
    );

    await expect(studioApi.runRecord("run-1")).rejects.toThrow(ZodError);
  });

  it("pins a run via PATCH", async () => {
    server.use(
      http.patch(`${BASE}/api/runs/history/run-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ pinned: true });
        return HttpResponse.json({ ok: true, pinned: true });
      }),
    );

    const result = await studioApi.pinRun("run-1", true);

    expect(result).toEqual({ ok: true, pinned: true });
  });

  it("rejects a pin answer with a wrong-typed pinned field", async () => {
    server.use(
      http.patch(`${BASE}/api/runs/history/run-1`, () =>
        HttpResponse.json({ ok: true, pinned: "yes" }),
      ),
    );

    await expect(studioApi.pinRun("run-1", true)).rejects.toThrow(ZodError);
  });

  it("deletes a run via DELETE", async () => {
    server.use(
      http.delete(`${BASE}/api/runs/history/run-1`, () => HttpResponse.json({ ok: true })),
    );

    await expect(studioApi.deleteRun("run-1")).resolves.toEqual({ ok: true });
  });

  it("rejects a delete answer with a wrong-typed ok field", async () => {
    server.use(
      http.delete(`${BASE}/api/runs/history/run-1`, () => HttpResponse.json({ ok: "yes" })),
    );

    await expect(studioApi.deleteRun("run-1")).rejects.toThrow(ZodError);
  });
});

describe("a refused save's own reading", () => {
  it("hands back the findings the backend named, field by field", async () => {
    server.use(
      http.post(`${BASE}/api/save`, () =>
        HttpResponse.json(
          {
            ok: false,
            detail: "This workflow is not described yet.",
            readiness: {
              executable: true,
              described: false,
              findings: [
                { field: "purpose", severity: "error", message: "Say what this workflow is for." },
              ],
            },
          },
          { status: 400 },
        ),
      ),
    );

    const reading = await studioApi.save().catch((error: unknown) => refusedReadiness(error));

    expect(reading).toEqual({
      executable: true,
      described: false,
      findings: [
        { field: "purpose", severity: "error", message: "Say what this workflow is for." },
      ],
    });
  });

  it("reads nothing into a failure that carried no reading", async () => {
    server.use(
      http.post(`${BASE}/api/save`, () =>
        HttpResponse.json({ ok: false, detail: "the draft does not validate" }, { status: 400 }),
      ),
    );

    const reading = await studioApi.save().catch((error: unknown) => refusedReadiness(error));

    expect(reading).toBeNull();
  });

  it("sends only the card fields a caller named", async () => {
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/workflow/wf.x.v1/card`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json({
          archived_at: null,
          ok: true,
          id: "wf.x.v1",
          title: "A better title",
          summary: "s",
          applicability: "a",
          provisional: true,
        });
      }),
    );

    await studioApi.redescribeWorkflow("wf.x.v1", { title: "A better title" });

    expect(patched).toEqual({ title: "A better title" });
  });
});
