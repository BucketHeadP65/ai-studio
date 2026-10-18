import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { server } from "@/test-support/msw/server";

import { conversationPayloads, inspectionApi } from "./inspection";

// The inspection surface rides the same harness backend (:8770) as `studioApi`, faked here as
// http://studio.test (vitest.config env). Each payload mirrors what
// `harness/src/harness/features/inspection/controller.py` actually answers.

const BASE = "http://studio.test";

function findingFixture(overrides: Record<string, unknown> = {}) {
  return {
    finding_id: 1,
    lead_id: 1,
    question_id: 1,
    source: "record:run-1",
    symptom: "warning_ignored",
    symptom_text: "the transcript shows a warning the agent never acted on",
    explanation: "The agent saw a warning in the tool's return and answered as though it never happened.",
    found_by: "inspector",
    model: "claude-test",
    decided_at: "2026-09-10T12:00:00+00:00",
    blames: [
      {
        root_cause: "tool_design",
        root_cause_text: "a tool's own description asked for a check its code never made",
        surface_kind: "tool",
        surface_name: "write_note",
        why: "the description promised a check the tool never made",
      },
    ],
    lead_found_by: "inspector",
    ...overrides,
  };
}

function leadFixture() {
  return {
    lead_id: 1,
    source: "record:run-1",
    watch: "every-run",
    symptom: "warning_ignored",
    spans: ["0.1.0"],
    found_by: "inspector",
    seen_at: "2026-09-10T12:00:00+00:00",
    attempted_at: null,
    refusal: null,
  };
}

function questionFixture() {
  return {
    question_id: 1,
    conversation_id: "44444444-4444-4444-8444-444444444444",
    kind: "attribute",
    source: "record:run-1",
    watch: "every-run",
    lead_id: 1,
    tier: "helper",
    model: "claude-test",
    asked_at: "2026-09-10T12:00:00+00:00",
    answer: "lead 1: finding 1 by claude-test",
  };
}

describe("inspectionApi", () => {
  it("lists findings and unwraps the envelope", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () =>
        HttpResponse.json({ findings: [findingFixture()] }),
      ),
    );

    const findings = await inspectionApi.findings();

    expect(findings).toHaveLength(1);
    expect(findings[0]?.symptom).toBe("warning_ignored");
    expect(findings[0]?.blames[0]?.root_cause).toBe("tool_design");
  });

  it("sends only the filters it was given", async () => {
    let seen = "";
    server.use(
      http.get(`${BASE}/api/inspection/findings`, ({ request }) => {
        seen = new URL(request.url).search;
        return HttpResponse.json({ findings: [] });
      }),
    );

    await inspectionApi.findings({ symptom: "warning_ignored", limit: 10 });

    expect(seen).toBe("?symptom=warning_ignored&limit=10");
  });

  it("reads one finding with its lead and its rendered evidence", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1`, () =>
        HttpResponse.json({
          finding: findingFixture(),
          lead: leadFixture(),
          evidence: [
            { span: "0.1.0", label: "step 0, the model's call to write_note", kind: "tool-call", text: "path: a.md" },
          ],
        }),
      ),
    );

    const detail = await inspectionApi.finding(1);

    expect(detail.lead?.spans).toEqual(["0.1.0"]);
    expect(detail.evidence[0]?.label).toBe("step 0, the model's call to write_note");
    expect(detail.evidence[0]?.text).toBe("path: a.md");
  });

  it("reads one question and its conversation", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/questions/1`, () =>
        HttpResponse.json({
          question: questionFixture(),
          messages: [
            {
              run_id: "inspector-a1b2",
              idx: 0,
              kind: "request",
              payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "Symptom: warning_ignored." }] },
              text: "Symptom: warning_ignored.",
            },
          ],
        }),
      ),
    );

    const page = await inspectionApi.question(1);

    expect(page.question.tier).toBe("helper");
    expect(page.messages[0]?.text).toBe("Symptom: warning_ignored.");
    expect(conversationPayloads(page.messages)[0]?.parts[0]?.part_kind).toBe("user-prompt");
  });

  it("drops a message whose payload this app cannot read rather than failing the page", () => {
    const payloads = conversationPayloads([
      { run_id: "r", idx: 0, kind: "request", payload: "not a message", text: "" },
      { run_id: "r", idx: 1, kind: "response", payload: { kind: "response", parts: [] }, text: "" },
    ]);

    expect(payloads).toHaveLength(1);
    expect(payloads[0]?.kind).toBe("response");
  });

  it("keeps a recorded request whose timestamp is null, so its tool return pairs with its call", () => {
    const payloads = conversationPayloads([
      {
        run_id: "r",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          timestamp: null,
          parts: [{ part_kind: "tool-return", tool_name: "read", tool_call_id: "c1", content: "5.8.0 said hi", timestamp: null }],
        },
        text: "5.8.0 said hi",
      },
    ]);

    expect(payloads).toHaveLength(1);
    expect(payloads[0]?.parts[0]?.tool_call_id).toBe("c1");
  });

  it("reads the turn under investigation, its messages and which of them were cited", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings/1/turn`, () =>
        HttpResponse.json({
          turns: [
            {
              step: 0,
              messages: [
                {
                  run_id: "run-1",
                  idx: 11,
                  kind: "response",
                  payload: {
                    kind: "response",
                    parts: [{ part_kind: "tool-call", tool_name: "fetch_workflow_spec", tool_call_id: "call-1", args: {} }],
                  },
                  text: "",
                },
              ],
              cited: [0],
              first: 11,
            },
          ],
        }),
      ),
    );

    const turn = await inspectionApi.turn(1);

    expect(turn.turns).toHaveLength(1);
    expect(turn.turns[0]?.step).toBe(0);
    expect(turn.turns[0]?.cited).toEqual([0]);
    expect(turn.turns[0]?.first).toBe(11);
  });

  it("reads one run's leads, findings, questions and jobs", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/run-1`, () =>
        HttpResponse.json({
          source: "record:run-1",
          leads: [leadFixture()],
          findings: [findingFixture()],
          questions: [questionFixture()],
          jobs: [
            {
              id: 1,
              kind: "inspect_run",
              key: "every-run:record:run-1",
              state: "done",
              attempts: 1,
              result: "record:run-1: 1 leads, 0 findings, 1 queued",
              last_error: null,
              created_at: "2026-09-10T12:00:00+00:00",
              finished_at: "2026-09-10T12:00:01+00:00",
            },
          ],
        }),
      ),
    );

    const overview = await inspectionApi.run("run-1");

    expect(overview.jobs[0]?.state).toBe("done");
    expect(overview.questions[0]?.kind).toBe("attribute");
  });

  it("asks the inspector and answers its words", async () => {
    let body = "";
    server.use(
      http.post(`${BASE}/api/inspection/questions/1/ask`, async ({ request }) => {
        body = await request.text();
        return HttpResponse.json({
          answer: "Because the description promised it.",
          conversation_id: "c-1",
          model: "claude-test",
        });
      }),
    );

    const said = await inspectionApi.ask(1, "why the tool description?");

    expect(said.answer).toBe("Because the description promised it.");
    expect(said.model).toBe("claude-test");
    expect(JSON.parse(body)).toEqual({ text: "why the tool description?" });
  });

  it("fails at the boundary when a finding drops a field", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () =>
        HttpResponse.json({ findings: [{ ...findingFixture(), symptom: undefined }] }),
      ),
    );

    await expect(inspectionApi.findings()).rejects.toBeInstanceOf(ZodError);
  });

  it("reads a run's outline", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/run-1/outline`, () =>
        HttpResponse.json({
          source: "record:run-1",
          agent: "harness-author",
          started_at: "2026-09-10T12:02:00+00:00",
          ended_at: "2026-09-10T12:06:12+00:00",
          ended: "run_completed",
          model: "z-ai/glm-5.3",
          tool_calls: 11,
          steps: [{ step: 0, messages: 2, summary: "asked 'run the digest'" }],
        }),
      ),
    );

    const outline = await inspectionApi.outline("run-1");

    expect(outline.agent).toBe("harness-author");
    expect(outline.steps[0]?.messages).toBe(2);
  });

  it("reads the messages one step added, in the turn's shape", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/runs/run-1/steps/3`, () =>
        HttpResponse.json({ step: 3, messages: [], cited: [], first: 8 }),
      ),
    );

    const step = await inspectionApi.step("run-1", 3);

    expect(step.first).toBe(8);
  });
});
