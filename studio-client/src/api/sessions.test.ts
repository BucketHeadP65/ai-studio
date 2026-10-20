import { http, HttpResponse, type JsonBodyType } from "msw";
import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { server } from "@/test-support/msw/server";

import { launchWorkflowRun, reconcileRun, sessionsApi, transcriptToUiMessages } from "./sessions";

// The sessions API rides the agent backend (:8770), faked here as http://studio.test
// (vitest.config env). Each test pins one endpoint's zod boundary with a payload
// mirroring the FastAPI routes' actual JSON, so a backend contract drift fails HERE,
// named, rather than as an undefined deep in a component.

const BASE = "http://studio.test";

interface RawSession {
  id: string;
  project_id: string;
  title: string;
  started_at: string;
  last_activity_at: string;
  archived: boolean;
  answering: boolean;
  turn_count: number;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
  };
  kind: "chat" | "runner" | "master";
  cost_usd: string;
  unpriced: number;
  context: {
    used_tokens: number;
    window_tokens: number;
    fraction: number;
    resolved: boolean;
  } | null;
}

function sessionFixture(overrides: Partial<RawSession> = {}): RawSession {
  return {
    id: "sess-1",
    project_id: "proj-1",
    title: "first steps",
    started_at: "2026-08-23T10:00:00Z",
    last_activity_at: "2026-08-23T10:05:00Z",
    archived: false,
    answering: true,
    turn_count: 2,
    usage: { input_tokens: 10, output_tokens: 20, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "chat",
    cost_usd: "0",
    unpriced: 0,
    context: null,
    ...overrides,
  };
}

function stubFetch(body: JsonBodyType): void {
  server.use(http.get(`${BASE}/api/sessions`, () => HttpResponse.json(body)));
}

describe("sessionsApi", () => {
  it("lists sessions and parses datetimes strictly", async () => {
    stubFetch({ sessions: [sessionFixture()] });
    const sessions = await sessionsApi.list();
    expect(sessions[0]?.title).toBe("first steps");
  });

  it("parses `answering`, the field that replaced `live`", async () => {
    stubFetch({ sessions: [sessionFixture({ answering: true })] });
    const sessions = await sessionsApi.list();
    expect(sessions[0]?.answering).toBe(true);
  });

  it("rejects a payload still shaped as `live`, not `answering`", async () => {
    const { answering, ...withoutAnswering } = sessionFixture();
    stubFetch({ sessions: [{ ...withoutAnswering, live: true }] });
    await expect(sessionsApi.list()).rejects.toThrow(ZodError);
  });

  it("rejects a malformed datetime at the boundary", async () => {
    stubFetch({ sessions: [{ ...sessionFixture(), started_at: "yesterday" }] });
    await expect(sessionsApi.list()).rejects.toThrow(ZodError);
  });

  it("parses a session's kind", async () => {
    stubFetch({ sessions: [sessionFixture({ kind: "runner" })] });
    const sessions = await sessionsApi.list();
    expect(sessions[0]?.kind).toBe("runner");
  });

  it("parses master kind, cost, and context vitals on a summary", async () => {
    stubFetch({
      sessions: [
        {
          ...sessionFixture(),
          kind: "master",
          cost_usd: "0.0421",
          context: { used_tokens: 8000, window_tokens: 128000, fraction: 0.0625, resolved: true },
        },
      ],
    });
    const [session] = await sessionsApi.list();
    expect(session?.kind).toBe("master");
    expect(session?.cost_usd).toBe("0.0421");
    expect(session?.context?.fraction).toBeCloseTo(0.0625);
  });

  it("builds the query string only from provided options", async () => {
    let seenUrl = "";
    server.use(
      http.get(`${BASE}/api/sessions`, ({ request }) => {
        seenUrl = request.url;
        return HttpResponse.json({ sessions: [] });
      }),
    );

    await sessionsApi.list({ q: "hello" });

    const url = new URL(seenUrl);
    expect(url.searchParams.get("q")).toBe("hello");
    expect(url.searchParams.has("archived")).toBe(false);
  });

  it("carries kind on the query string, so a caller can ask past the backend's chat-only default", async () => {
    let seenUrl = "";
    server.use(
      http.get(`${BASE}/api/sessions`, ({ request }) => {
        seenUrl = request.url;
        return HttpResponse.json({ sessions: [] });
      }),
    );

    await sessionsApi.list({ kind: "all" });

    const url = new URL(seenUrl);
    expect(url.searchParams.get("kind")).toBe("all");
  });

  it("reads one session's detail with its turns", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/sess-1`, () =>
        HttpResponse.json({ session: sessionFixture(), turns: [{ seq: 1, entry: "chat" }] }),
      ),
    );

    const detail = await sessionsApi.detail("sess-1");

    expect(detail.session.id).toBe("sess-1");
    expect(detail.turns).toEqual([{ seq: 1, entry: "chat" }]);
  });

  it("reads a session's transcript as messages", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/sess-1/transcript`, () =>
        HttpResponse.json({
          messages: [{ run_id: "r1", idx: 0, kind: "request", payload: {}, text: "hello" }],
        }),
      ),
    );

    const messages = await sessionsApi.transcript("sess-1");

    expect(messages).toEqual([
      { run_id: "r1", idx: 0, kind: "request", payload: {}, text: "hello" },
    ]);
  });

  it("creates a session with no body when no project is named, and unwraps it", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ session: sessionFixture({ id: "sess-new", answering: false }) });
      }),
    );

    const created = await sessionsApi.create();

    expect(posted).toEqual({});
    expect(created.id).toBe("sess-new");
  });

  it("creates a session on a named project", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ session: sessionFixture({ project_id: "proj-9" }) });
      }),
    );

    await sessionsApi.create("proj-9");

    expect(posted).toEqual({ project_id: "proj-9" });
  });

  it("creates a runner session, unarchived, so a manual run relies on the kind filter alone", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ session: sessionFixture({ kind: "runner" }) });
      }),
    );

    const created = await sessionsApi.create(undefined, "runner");

    expect(posted).toEqual({ kind: "runner" });
    expect(created.kind).toBe("runner");
  });

  it("interrupts a session's in-flight turn", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/interrupt`, () => HttpResponse.json({ ok: true })),
    );

    await expect(sessionsApi.interrupt("sess-1")).resolves.toEqual({ ok: true });
  });

  it("steers a session's in-flight turn with the message", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/steer`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    await expect(sessionsApi.steer("sess-1", "also check the tests")).resolves.toEqual({ ok: true });
    expect(posted).toEqual({ message: "also check the tests" });
  });

  it("reads a session's conversation, turns plus the draft spread beside them", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/sess-1/conversation`, () =>
        HttpResponse.json({
          turns: [{ who: "you", text: "draft me a check" }],
          draft: { spec_yaml: "name: x\n", title: "a check", summary: "", applicability: "" },
          issues: [],
          valid: true,
          graph: { name: "x", version: 1, nodes: [], edges: [] },
        }),
      ),
    );

    const conversation = await sessionsApi.conversation("sess-1");

    expect(conversation.turns).toEqual([{ who: "you", text: "draft me a check" }]);
    expect(conversation.draft.title).toBe("a check");
    expect(conversation.valid).toBe(true);
    expect(conversation.graph?.name).toBe("x");
  });

  it("lists the live runs, each naming its session and embedding its gate", async () => {
    server.use(
      http.get(`${BASE}/api/runs`, () =>
        HttpResponse.json({
          runs: [
            {
              run_title: "",
              started_at: "",
              session_id: "sess-1",
              workflow_id: "wf.lit-check.v5",
              state: "waiting",
              active: [],
              done: ["intake"],
              failed: [],
              waiting_at: "review",
              gate: {
                gate_id: "review",
                workflow: "wf.lit-check.v5",
                reviewing: "{}",
                rejection_reruns: "compose",
              },
            },
          ],
        }),
      ),
    );

    const runs = await sessionsApi.runs();

    expect(runs).toHaveLength(1);
    expect(runs[0]?.session_id).toBe("sess-1");
    expect(runs[0]?.gate?.gate_id).toBe("review");
  });

  it("rejects a run view missing the gate key entirely", async () => {
    server.use(
      http.get(`${BASE}/api/runs`, () =>
        HttpResponse.json({
          runs: [
            {
              run_title: "",
              started_at: "",
              session_id: "sess-1",
              workflow_id: "wf.lit-check.v5",
              state: "running",
              active: ["draft"],
              done: [],
              failed: [],
              waiting_at: null,
            },
          ],
        }),
      ),
    );

    await expect(sessionsApi.runs()).rejects.toThrow(ZodError);
  });

  it("starts a session-scoped run, posting the workflow id and arguments", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );

    await sessionsApi.run("sess-1", "wf.lit-check.v5", { context: "a payments system" });

    expect(posted).toEqual({
      workflow_id: "wf.lit-check.v5",
      arguments: { context: "a payments system" },
    });
  });

  it("starts a run with gate_mode when the caller hands gates to the orchestrator", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );

    await sessionsApi.run(
      "sess-1",
      "wf.lit-check.v5",
      { context: "a payments system" },
      "orchestrate",
    );

    expect(posted).toEqual({
      workflow_id: "wf.lit-check.v5",
      arguments: { context: "a payments system" },
      gate_mode: "orchestrate",
    });
  });

  it("starts a run with gate_mode auto when the caller asks gates to pass themselves", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );

    await sessionsApi.run("sess-1", "wf.lit-check.v5", { context: "a payments system" }, "auto");

    expect(posted).toEqual({
      workflow_id: "wf.lit-check.v5",
      arguments: { context: "a payments system" },
      gate_mode: "auto",
    });
  });

  it("sends nothing extra for gate_mode wait, same as omitting it", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v5" });
      }),
    );

    await sessionsApi.run("sess-1", "wf.lit-check.v5", { context: "a payments system" }, "wait");

    expect(posted).toEqual({
      workflow_id: "wf.lit-check.v5",
      arguments: { context: "a payments system" },
    });
  });

  it("decides a session-scoped gate", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, async ({ request }) => {
        const body = (await request.json()) as { approve: boolean };
        return HttpResponse.json({ ok: true, approved: body.approve });
      }),
    );

    const decided = await sessionsApi.decide("sess-1", true);

    expect(decided).toEqual({ ok: true, approved: true });
  });

  it("posts a session-scoped rejection with feedback", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, approved: false });
      }),
    );

    await sessionsApi.decide("sess-1", false, "the risk section is thin");

    expect(posted).toEqual({ approve: false, feedback: "the risk section is thin" });
  });

  it("omits feedback from the body when none is given", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, approved: true });
      }),
    );

    await sessionsApi.decide("sess-1", true);

    expect(posted).toEqual({ approve: true });
  });

  it("flips automode on a session-scoped run", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/automode`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, auto_approve: true });
      }),
    );

    const result = await sessionsApi.setAutomode("sess-1", true);

    expect(posted).toEqual({ on: true });
    expect(result).toEqual({ ok: true, auto_approve: true });
  });

  it("reads a session-scoped run state", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/sess-1/run/state`, () =>
        HttpResponse.json({
          auto_approve: false,
          gate_mode: "wait",
          running: true,
          workflow: "wf.lit-check.v5",
          result: null,
          active: ["draft"],
          done: [],
          failed: [],
          waiting_at: null,
          events: [],
          seq: 1,
        }),
      ),
    );

    const state = await sessionsApi.runState("sess-1");

    expect(state.running).toBe(true);
    expect(state.workflow).toBe("wf.lit-check.v5");
  });

  it("stops a session-scoped run", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/stop`, () =>
        HttpResponse.json({ ok: true, stopped: "wf.lit-check.v5" }),
      ),
    );

    const stopped = await sessionsApi.stopRun("sess-1");

    expect(stopped).toEqual({ ok: true, stopped: "wf.lit-check.v5" });
  });

  it("rejects a stop answer with a wrong-typed stopped field", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/stop`, () =>
        HttpResponse.json({ ok: true, stopped: 42 }),
      ),
    );

    await expect(sessionsApi.stopRun("sess-1")).rejects.toThrow(ZodError);
  });

  it("renames a session via PATCH and unwraps the session", async () => {
    server.use(
      http.patch(`${BASE}/api/sessions/sess-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ title: "new title" });
        return HttpResponse.json({ session: sessionFixture({ title: "new title" }) });
      }),
    );

    const session = await sessionsApi.rename("sess-1", "new title");

    expect(session.title).toBe("new title");
  });

  it("archives a session via PATCH and unwraps the session", async () => {
    server.use(
      http.patch(`${BASE}/api/sessions/sess-1`, async ({ request }) => {
        const body = await request.json();
        expect(body).toEqual({ archived: true });
        return HttpResponse.json({ session: sessionFixture({ archived: true }) });
      }),
    );

    const session = await sessionsApi.setArchived("sess-1", true);

    expect(session.archived).toBe(true);
  });

  it("removes a session, accepting a 204 with no body", async () => {
    server.use(
      http.delete(`${BASE}/api/sessions/sess-1`, () => new HttpResponse(null, { status: 204 })),
    );

    await expect(sessionsApi.remove("sess-1")).resolves.toBeUndefined();
  });

  it("lists the known projects", async () => {
    server.use(
      http.get(`${BASE}/api/projects`, () =>
        HttpResponse.json({ projects: [{ id: "proj-1", name: "engram", root_path: "/repo" }] }),
      ),
    );

    const projects = await sessionsApi.projects();

    expect(projects).toEqual([{ id: "proj-1", name: "engram", root_path: "/repo" }]);
  });

  it("resolves the master session id", async () => {
    server.use(http.get(`${BASE}/api/master`, () => HttpResponse.json({ session_id: "m-1" })));
    expect(await sessionsApi.master()).toBe("m-1");
  });

  it("forgets a session's conversation by id", async () => {
    server.use(
      http.delete(`${BASE}/api/sessions/m-1/conversation`, () => HttpResponse.json({ ok: true })),
    );
    expect(await sessionsApi.forget("m-1")).toEqual({ ok: true });
  });
});

describe("launchWorkflowRun", () => {
  interface Launched {
    created: unknown;
    renamed: unknown;
    ran: unknown;
  }

  function mountLaunch(): Launched {
    const wire: Launched = { created: null, renamed: null, ran: null };
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        wire.created = await request.json();
        return HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) });
      }),
      http.patch(`${BASE}/api/sessions/sess-new`, async ({ request }) => {
        wire.renamed = await request.json();
        return HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) });
      }),
      http.post(`${BASE}/api/sessions/sess-new/run`, async ({ request }) => {
        wire.ran = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.decide.v2" });
      }),
    );
    return wire;
  }

  it("opens a runner session named for the container, then runs the intent in it", async () => {
    const wire = mountLaunch();

    const outcome = await launchWorkflowRun(
      {
        workflowId: "wf.decide.v2",
        runTitle: "Decide with the literature, which cache",
        arguments: { question: "Which cache?", knowledge_bases: ["research"], publish: false },
        gateMode: "wait",
      },
      "Decide with the literature",
    );

    expect(outcome).toEqual({ state: "started", sessionId: "sess-new" });
    expect(wire.created).toEqual({ kind: "runner" });
    expect(wire.renamed).toEqual({ title: "Decide with the literature" });
    // The run route carries the workflow id, the arguments as their own JSON types, the run's
    // own title, and the gate mode when it is not the wait every backend already defaults to.
    expect(wire.ran).toEqual({
      workflow_id: "wf.decide.v2",
      arguments: { question: "Which cache?", knowledge_bases: ["research"], publish: false },
      run_title: "Decide with the literature, which cache",
    });
  });

  it("names the run apart from the session that contains it", async () => {
    const wire = mountLaunch();

    await launchWorkflowRun(
      { workflowId: "wf.decide.v2", runTitle: "Which cache do we use?", arguments: {}, gateMode: "wait" },
      "Decide with the literature",
    );

    expect(wire.renamed).toEqual({ title: "Decide with the literature" });
    expect(wire.ran).toEqual({
      workflow_id: "wf.decide.v2",
      arguments: {},
      run_title: "Which cache do we use?",
    });
  });

  it("sends no title at all when the intent carries none", async () => {
    const wire = mountLaunch();

    await launchWorkflowRun(
      { workflowId: "wf.decide.v2", runTitle: "", arguments: {}, gateMode: "wait" },
      "Decide with the literature",
    );

    expect(wire.ran).toEqual({ workflow_id: "wf.decide.v2", arguments: {} });
  });

  it("sends the gate mode when the intent names one other than waiting", async () => {
    const wire = mountLaunch();

    await launchWorkflowRun(
      { workflowId: "wf.decide.v2", runTitle: "Decide", arguments: {}, gateMode: "auto" },
      "Decide",
    );

    expect(wire.ran).toEqual({
      workflow_id: "wf.decide.v2",
      arguments: {},
      gate_mode: "auto",
      run_title: "Decide",
    });
  });

  it("reports an unanswered start as unknown, with the session to settle it against", async () => {
    let starts = 0;
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) }),
      ),
      http.patch(`${BASE}/api/sessions/sess-new`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) }),
      ),
      http.post(`${BASE}/api/sessions/sess-new/run`, () => {
        starts += 1;
        return HttpResponse.error();
      }),
    );

    const outcome = await launchWorkflowRun(
      { workflowId: "wf.decide.v2", runTitle: "Decide", arguments: {}, gateMode: "wait" },
      "Decide",
    );

    expect(outcome.state).toBe("unknown");
    expect(outcome.sessionId).toBe("sess-new");
    expect(starts).toBe(1);
  });

  it("throws a refusal the backend actually answered with, since that is something to correct", async () => {
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) }),
      ),
      http.patch(`${BASE}/api/sessions/sess-new`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-new", kind: "runner" }) }),
      ),
      http.post(`${BASE}/api/sessions/sess-new/run`, () =>
        HttpResponse.json({ ok: false, message: "fill in: question" }, { status: 400 }),
      ),
    );

    await expect(
      launchWorkflowRun(
        { workflowId: "wf.decide.v2", runTitle: "Decide", arguments: {}, gateMode: "wait" },
        "Decide",
      ),
    ).rejects.toThrow();
  });
});

describe("reconcileRun", () => {
  function runView(sessionId: string) {
    return {
      session_id: sessionId,
      workflow_id: "wf.decide.v2",
      run_title: "Decide",
      state: "running",
      active: [],
      done: [],
      failed: [],
      waiting_at: null,
      gate: null,
      started_at: "2026-09-16T09:00:00Z",
    };
  }

  function endedRun(sessionId: string) {
    return {
      spend: null,
      result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
      run_id: "run-7",
      workflow_id: "wf.decide.v2",
      run_title: "Decide",
      outcome: "completed",
      failure: "",
      started_at: "2026-09-16T09:00:00Z",
      ended_at: "2026-09-16T09:06:00Z",
      runner: "app:harness",
      session_id: sessionId,
      params: { question: "Which cache?", knowledge_bases: ["research"] },
      gates_approved: [],
      gates_rejected: {},
      gates_auto: [],
      gates_orchestrated: [],
      pinned: false,
    };
  }

  it("finds the run still in flight in that session", async () => {
    server.use(http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [runView("sess-new")] })));

    expect(await reconcileRun("sess-new")).toEqual({
      sessionId: "sess-new",
      workflowId: "wf.decide.v2",
      runId: null,
      live: true,
    });
  });

  it("falls through to the recent history for a run that already ended", async () => {
    server.use(
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: [endedRun("sess-new")] })),
    );

    expect(await reconcileRun("sess-new")).toEqual({
      sessionId: "sess-new",
      workflowId: "wf.decide.v2",
      runId: "run-7",
      live: false,
    });
  });

  it("answers with nothing when neither read names that session, so nothing started", async () => {
    server.use(
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: [endedRun("another")] })),
    );

    expect(await reconcileRun("sess-new")).toBeNull();
  });
});

describe("transcriptToUiMessages", () => {
  function userPrompt(text: string) {
    return { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] };
  }

  function toolCall(toolName: string, args: Record<string, unknown>) {
    return { kind: "response", parts: [{ part_kind: "tool-call", tool_name: toolName, args }] };
  }

  function toolReturn(toolName: string, content: unknown) {
    return { kind: "request", parts: [{ part_kind: "tool-return", tool_name: toolName, content }] };
  }

  function assistantText(text: string) {
    return { kind: "response", parts: [{ part_kind: "text", content: text }] };
  }

  it("keeps only the user-prompt and text parts, dropping tool traffic entirely", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("hello"), text: "hello" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: toolCall("load_skill", { name: "docs" }),
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "request",
        payload: toolReturn("load_skill", "a".repeat(14000)),
        text: "",
      },
      {
        run_id: "r1",
        idx: 3,
        kind: "response",
        payload: assistantText("hi there"),
        text: "hi there",
      },
    ]);

    expect(messages).toHaveLength(2);
    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(messages[0]?.id).toBe("r1:0");
    expect(messages[1]?.id).toBe("r1:3");
    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "hi there" },
    ]);
  });

  it("joins multiple user-prompt parts on one request message", () => {
    const messages = transcriptToUiMessages([
      {
        run_id: "r1",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [
            { part_kind: "user-prompt", content: "part one " },
            { part_kind: "user-prompt", content: "part two" },
          ],
        },
        text: "part one part two",
      },
    ]);

    expect(messages[0]?.parts).toEqual([{ type: "text", text: "part one part two" }]);
  });

  it("drops a message whose payload doesn't parse against the tolerant schema", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: null, text: "hello" },
    ]);

    expect(messages).toEqual([]);
  });

  it("strips the page-context fence from transcript user bubbles", () => {
    const fenced =
      "<page-context>\nThe person is on chat at /chat.\n</page-context>\n\nwhat failed?";
    const messages = transcriptToUiMessages([
      {
        run_id: "r1",
        idx: 0,
        kind: "request",
        payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: fenced }] },
        text: fenced,
      },
    ]);
    expect(messages[0]?.parts).toEqual([{ type: "text", text: "what failed?" }]);
  });

  it("keeps companion tool calls and their returns on the assistant bubble", () => {
    const messages = transcriptToUiMessages([
      {
        run_id: "r1",
        idx: 0,
        kind: "request",
        payload: { kind: "request", parts: [{ part_kind: "user-prompt", content: "go" }] },
        text: "go",
      },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [
            {
              part_kind: "tool-call",
              tool_name: "navigate",
              args: { url: "/chat" },
              tool_call_id: "c-1",
            },
          ],
        },
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "request",
        payload: {
          kind: "request",
          parts: [
            {
              part_kind: "tool-return",
              tool_name: "navigate",
              content: "navigated to /chat",
              tool_call_id: "c-1",
            },
          ],
        },
        text: "",
      },
      {
        run_id: "r1",
        idx: 3,
        kind: "response",
        payload: {
          kind: "response",
          parts: [{ part_kind: "text", content: '{"message": "there you are"}' }],
        },
        text: "",
      },
    ]);
    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    expect(messages[1]?.id).toBe("r1:3");
    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      {
        type: "tool-navigate",
        toolCallId: "c-1",
        state: "output-available",
        input: { url: "/chat" },
        output: "navigated to /chat",
      },
      { type: "step-start" },
      { type: "text", text: '{"message": "there you are"}' },
    ]);
  });

  it("keeps a turn the person answered mid-way in one bubble across the run it resumed on", () => {
    const fenceOnly = "<page-context>\nThe person is on chat at /chat.\n</page-context>\n";
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [
            { part_kind: "text", content: "Switching the tab." },
            {
              part_kind: "tool-call",
              tool_name: "set_tab",
              args: { tab: "graph" },
              tool_call_id: "c-1",
            },
          ],
        },
        text: "Switching the tab.",
      },
      {
        run_id: "r2",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [
            {
              part_kind: "tool-return",
              tool_name: "set_tab",
              content: "switched",
              tool_call_id: "c-1",
            },
            { part_kind: "user-prompt", content: fenceOnly },
          ],
        },
        text: fenceOnly,
      },
      { run_id: "r2", idx: 1, kind: "response", payload: assistantText("Done."), text: "Done." },
    ]);

    expect(messages.map((m) => [m.role, m.id])).toEqual([
      ["user", "r1:0"],
      ["assistant", "r2:1"],
    ]);
    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "Switching the tab." },
      {
        type: "tool-set_tab",
        toolCallId: "c-1",
        state: "output-available",
        input: { tab: "graph" },
        output: "switched",
      },
      { type: "step-start" },
      { type: "text", text: "Done." },
    ]);
  });

  it("opens a new bubble only after the person's own words, whatever run they arrive on", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: assistantText("Reading."),
        text: "Reading.",
      },
      { run_id: "r1", idx: 2, kind: "response", payload: assistantText("Done."), text: "Done." },
      { run_id: "r2", idx: 0, kind: "request", payload: userPrompt("more"), text: "more" },
      { run_id: "r2", idx: 1, kind: "response", payload: assistantText("More."), text: "More." },
    ]);

    expect(messages.map((m) => [m.role, m.id])).toEqual([
      ["user", "r1:0"],
      ["assistant", "r1:2"],
      ["user", "r2:0"],
      ["assistant", "r2:1"],
    ]);
    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      { type: "text", text: "Reading." },
      { type: "step-start" },
      { type: "text", text: "Done." },
    ]);
  });

  it("closes a turn on a prompt whose payload will not parse, reading the words off the text column", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      { run_id: "r1", idx: 1, kind: "response", payload: assistantText("First."), text: "First." },
      { run_id: "r2", idx: 0, kind: "request", payload: null, text: "again" },
      {
        run_id: "r2",
        idx: 1,
        kind: "response",
        payload: assistantText("Second."),
        text: "Second.",
      },
    ]);

    expect(messages.map((m) => [m.role, m.id])).toEqual([
      ["user", "r1:0"],
      ["assistant", "r1:1"],
      ["assistant", "r2:1"],
    ]);
  });

  function usage(outputTokens: number) {
    return {
      input_tokens: 100,
      output_tokens: outputTokens,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    };
  }

  it("emits a data-step part right after step-start, from the row's own usage and timestamp", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [{ part_kind: "text", content: "Done." }],
          usage: usage(51),
          timestamp: "2026-08-28T23:04:22.206741Z",
        },
        text: "Done.",
      },
    ]);

    expect(messages[1]?.parts).toEqual([
      { type: "step-start" },
      {
        type: "data-step",
        data: {
          tool_call_ids: [],
          usage: usage(51),
          timestamp: "2026-08-28T23:04:22.206741Z",
        },
      },
      { type: "text", text: "Done." },
    ]);
  });

  it("emits no data-step for a row that carries no usage or timestamp", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      { run_id: "r1", idx: 1, kind: "response", payload: assistantText("Done."), text: "Done." },
    ]);

    expect(messages[1]?.parts).toEqual([{ type: "step-start" }, { type: "text", text: "Done." }]);
  });

  it("adds returns from the tool-return parts that answered the step's own calls", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [
            {
              part_kind: "tool-call",
              tool_name: "navigate",
              tool_call_id: "c-1",
              args: { url: "/chat" },
            },
          ],
          usage: usage(51),
          timestamp: "2026-08-28T23:04:22Z",
        },
        text: "",
      },
      {
        run_id: "r1",
        idx: 2,
        kind: "request",
        payload: {
          kind: "request",
          parts: [
            {
              part_kind: "tool-return",
              tool_name: "navigate",
              tool_call_id: "c-1",
              content: "navigated to /chat",
              timestamp: "2026-08-28T23:04:23Z",
            },
          ],
        },
        text: "",
      },
    ]);

    const dataStep = messages[1]?.parts.find((part) => part.type === "data-step");
    expect(dataStep).toEqual({
      type: "data-step",
      data: {
        tool_call_ids: ["c-1"],
        usage: usage(51),
        timestamp: "2026-08-28T23:04:22Z",
        returns: { "c-1": "2026-08-28T23:04:23Z" },
      },
    });
  });

  it("collects every tool call's id for the step, including a tool the bubble does not render", () => {
    const messages = transcriptToUiMessages([
      { run_id: "r1", idx: 0, kind: "request", payload: userPrompt("go"), text: "go" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [
            { part_kind: "text", content: "Reading." },
            {
              part_kind: "tool-call",
              tool_name: "read_file",
              tool_call_id: "c-9",
              args: { path: "a.md" },
            },
          ],
          usage: usage(8),
          timestamp: "2026-08-28T23:00:00Z",
        },
        text: "",
      },
    ]);

    const dataStep = messages[1]?.parts.find(
      (part): part is { type: "data-step"; data: unknown } => part.type === "data-step",
    );
    expect(dataStep?.data).toMatchObject({ tool_call_ids: ["c-9"] });
  });
});
