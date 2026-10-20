import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { runKeys, studioKeys } from "@/api/query-keys";
import type { RunView, SessionSummary } from "@/api/sessions-types";
import type { RunEnvelope, RunLimits, RunState, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the component sees the mocked module.
import { RunControls } from "./run-controls";

// RunControls owns whichever session is acting on this workflow: filling and
// firing a manual run (a fresh session, titled after the workflow), pausing
// and resuming it, answering its gate, and reading back what it produced.
// Every test states the network surface it needs; MSW errors on anything
// left unmocked.

const BASE = "http://studio.test";

const IDLE: RunState = {
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
};

const DETAIL: WorkflowDetail = {
  ok: true,
  id: "wf.lit-check.v5",
  archived_at: null,
  name: "lit-check",
  version: 5,
  description: "Check a decision against the literature.",
  title: "Check against the literature",
  summary: "Checks one decision against the literature.",
  applicability: "Use before a decision ships. Do not use for a broad survey.",
  provisional: false,
  graph: {
    name: "lit-check",
    version: 5,
    nodes: [
      {
        key: "param:context",
        id: "context",
        kind: "param",
        signature: "string",
        detail: "what the decision applies to",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: { type: "string", minLength: 1 },
        source: "",
      },
      {
        key: "draft",
        id: "draft",
        kind: "run",
        signature: "compose(context: string) → Record",
        detail: "",
        tools: [],
        required: true,
        is_output: true,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
      {
        key: "review",
        id: "review",
        kind: "gate",
        signature: "gate",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
    ],
    edges: [],
  },
  issues: [],
  fits: true,
  missing: [],
  brings: [],
  spec_yaml: "name: lit-check\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
  trust: {
    level: "machine-confirmed",
    runs_with_evidence: 2,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
};

// The same workflow with the author's own presentation. A setup composes a run's name out of the
// first declared input, so only a card that declares one can say what a rerun's name becomes.
const PRESENTED: WorkflowDetail = {
  ...DETAIL,
  presentation: {
    purpose: "",
    produces: "",
    owner_participation: "",
    not_for: "",
    inputs: [{ key: "context", label: "context", control: null }],
    result_fields: [],
    actions: [],
  },
};

function usage() {
  return { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 };
}

function sessionFixture(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: "sess-new",
    project_id: "proj-1",
    title: "session sess-new",
    started_at: "2026-08-23T10:00:00Z",
    last_activity_at: "2026-08-23T10:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: usage(),
    kind: "chat",
    cost_usd: "0",
    unpriced: 0,
    context: null,
    ...overrides,
  };
}

function paramId(value: string | readonly string[] | undefined): string {
  if (typeof value === "string") return value;
  return value?.[0] ?? "";
}

const BRAKES: RunLimits = {
  brakes: [
    { window: "run", usd: "2", warn_at: null },
    { window: "day", usd: "25", warn_at: 0.8 },
  ],
  gate_modes: ["wait", "orchestrate", "auto"],
  default_gate_mode: "wait",
};

function endedRun(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    run_id: "run-4",
    workflow_id: DETAIL.id,
    run_title: "Check against the literature, the old context",
    outcome: "completed",
    failure: "",
    started_at: "2026-09-16T09:00:00Z",
    ended_at: "2026-09-16T09:06:00Z",
    runner: "app:harness",
    session_id: "sess-old",
    params: { context: "the old context" },
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    spend: null,
    result: {
      status: "unknown",
      summary: "",
      attempted: 0,
      succeeded: 0,
      skipped: 0,
      failed: 0,
      retry_attempts: 0,
      primary_input: null,
      generation: null,
      checks: [],
      artifacts: [],
    },
    ...overrides,
  };
}

/** The newer of two ended runs, so a test can tell which one an act actually worked from. */
const NEWEST_RUN: RunEnvelope = endedRun({
  run_id: "run-7",
  run_title: "Check against the literature, the newest context",
  params: { context: "the newest context" },
  session_id: "sess-7",
});

function mountApi(
  overrides: {
    runs?: RunView[];
    runStates?: Record<string, RunState>;
    history?: RunEnvelope[];
    recent?: RunEnvelope[];
    limits?: RunLimits;
  } = {},
) {
  const runStates = overrides.runStates ?? {};
  server.use(
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: overrides.runs ?? [] })),
    http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: overrides.recent ?? [] })),
    http.get(`${BASE}/api/workflows/:id/history`, () =>
      HttpResponse.json({ runs: overrides.history ?? [] }),
    ),
    http.get(`${BASE}/api/run/limits`, () => HttpResponse.json(overrides.limits ?? BRAKES)),
    http.get(`${BASE}/api/sessions/:id/run/state`, ({ params }) =>
      HttpResponse.json(runStates[paramId(params.id)] ?? IDLE),
    ),
  );
}

/** The three calls one launch makes, with the start keeping the body it was actually sent. */
function capturedLaunch(): { body: unknown } {
  const sent: { body: unknown } = { body: null };
  server.use(
    http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
    http.patch(`${BASE}/api/sessions/:id`, () =>
      HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) }),
    ),
    http.post(`${BASE}/api/sessions/sess-new/run`, async ({ request }) => {
      sent.body = await request.json();
      return HttpResponse.json({ ok: true, running: DETAIL.id });
    }),
  );
  return sent;
}

/** The three calls one launch makes, with the start answering however the case needs it to. */
function mountLaunch(answer: () => Response): { starts: number } {
  const counted = { starts: 0 };
  server.use(
    http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
    http.patch(`${BASE}/api/sessions/:id`, () =>
      HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) }),
    ),
    http.post(`${BASE}/api/sessions/sess-new/run`, () => {
      counted.starts += 1;
      return answer();
    }),
  );
  return counted;
}

// Every launch goes through the setup dialog: open it, fill the one declared parameter,
// optionally answer the gate question, and confirm.
async function setUpRun(context: string, gateChoice?: string): Promise<void> {
  await userEvent.click(await screen.findByRole("button", { name: "Run" }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.type(await within(dialog).findByLabelText("context"), context);
  if (gateChoice !== undefined) {
    await userEvent.click(within(dialog).getByRole("radio", { name: gateChoice }));
  }
  await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));
}

describe("RunControls", () => {
  beforeEach(() => {
    routerPush.mockClear();
  });

  it("asks for its parameters in setup, never on the page behind it", async () => {
    mountApi();
    render(<RunControls detail={DETAIL} sessionId={null} />);

    expect(await screen.findByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.queryByLabelText("context")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByLabelText("context")).toBeInTheDocument();
  });

  it("manual run creates a fresh runner session, names it, then runs session-scoped", async () => {
    mountApi();
    let createdBody: unknown = null;
    let renamedId = "";
    const patched: unknown[] = [];
    let ranId = "";
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        createdBody = await request.json();
        return HttpResponse.json({ session: sessionFixture() });
      }),
      http.patch(`${BASE}/api/sessions/:id`, async ({ params, request }) => {
        renamedId = paramId(params.id);
        patched.push(await request.json());
        return HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) });
      }),
      http.post(`${BASE}/api/sessions/:id/run`, async ({ params, request }) => {
        ranId = paramId(params.id);
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a payments system");

    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.lit-check.v5",
        arguments: { context: "a payments system" },
        run_title: "Check against the literature",
      }),
    );
    // Created as a runner session, not archived: the backend's own `kind`
    // filter is what keeps it off the chat rail's default list.
    expect(createdBody).toEqual({ kind: "runner" });
    expect(renamedId).toBe("sess-new");
    // The session is the container this run lives in, so it carries the card's own name.
    expect(patched).toEqual([{ title: "Check against the literature" }]);
    expect(ranId).toBe("sess-new");
  });

  it("defaults the gates-on-this-run choice to wait for me, sending nothing extra", async () => {
    mountApi();
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
      http.patch(`${BASE}/api/sessions/:id`, () =>
        HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) }),
      ),
      http.post(`${BASE}/api/sessions/:id/run`, async ({ request }) => {
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("radio", { name: "Wait for me" })).toBeChecked();
    await userEvent.type(await within(dialog).findByLabelText("context"), "a payments system");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.lit-check.v5",
        arguments: { context: "a payments system" },
        run_title: "Check against the literature",
      }),
    );
  });

  it("picking the orchestrator decides posts gate_mode orchestrate", async () => {
    mountApi();
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
      http.patch(`${BASE}/api/sessions/:id`, () =>
        HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) }),
      ),
      http.post(`${BASE}/api/sessions/:id/run`, async ({ request }) => {
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a payments system", "The orchestrator decides");

    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.lit-check.v5",
        arguments: { context: "a payments system" },
        gate_mode: "orchestrate",
        run_title: "Check against the literature",
      }),
    );
  });

  it("picking pass automatically posts gate_mode auto", async () => {
    mountApi();
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
      http.patch(`${BASE}/api/sessions/:id`, () =>
        HttpResponse.json({ session: sessionFixture({ title: "lit-check" }) }),
      ),
      http.post(`${BASE}/api/sessions/:id/run`, async ({ request }) => {
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a payments system", "Pass automatically");

    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.lit-check.v5",
        arguments: { context: "a payments system" },
        gate_mode: "auto",
        run_title: "Check against the literature",
      }),
    );
  });

  it("uses only the house's allowed punctuation on the gates-on-this-run choice", async () => {
    mountApi();
    render(<RunControls detail={DETAIL} sessionId={null} />);
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    const legend = within(dialog).getByText("Gates on this run");
    expect(legend.textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
    for (const name of ["Wait for me", "The orchestrator decides", "Pass automatically"]) {
      expect(within(dialog).getByRole("radio", { name }).parentElement?.textContent ?? "").toMatch(
        ALLOWED_PUNCTUATION,
      );
    }
  });

  it("shows the run's message when starting it is refused", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/sessions`, () => HttpResponse.json({ session: sessionFixture() })),
      http.patch(`${BASE}/api/sessions/sess-new`, () =>
        HttpResponse.json({ session: sessionFixture() }),
      ),
      http.post(`${BASE}/api/sessions/sess-new/run`, () =>
        HttpResponse.json({ detail: "wf.lit-check.v5 is still running" }, { status: 409 }),
      ),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a payments system");

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "wf.lit-check.v5 is still running",
    );
    expect(within(dialog).getByLabelText("context")).toHaveValue("a payments system");
  });

  it("offers Pause only while the acting session's run is running, and posts it session-scoped", async () => {
    mountApi({ runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id } } });
    let pausedOnSess1 = false;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/pause`, () => {
        pausedOnSess1 = true;
        return HttpResponse.json({ ok: true, paused: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    const pauseButton = await screen.findByRole("button", { name: "Pause" });
    await userEvent.click(pauseButton);
    await waitFor(() => expect(pausedOnSess1).toBe(true));
  });

  it("offers no Pause when there is no acting session for this workflow", async () => {
    mountApi();
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await screen.findByRole("button", { name: "Run" });
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  });

  it("offers Resume only once a pause has landed, and posts it session-scoped", async () => {
    mountApi({ runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id } } });
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/pause`, () =>
        HttpResponse.json({ ok: true, paused: DETAIL.id }),
      ),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    const resumeButton = await screen.findByRole("button", { name: "Resume" });

    let resumedOnSess1 = false;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/resume`, () => {
        resumedOnSess1 = true;
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    await userEvent.click(resumeButton);
    await waitFor(() => expect(resumedOnSess1).toBe(true));
  });

  it("shows the gate panel when the acting session's run is waiting, and decides through it session-scoped", async () => {
    mountApi({
      runs: [
        {
          session_id: "sess-1",
          workflow_id: DETAIL.id,
          run_title: "",
          state: "waiting",
          active: [],
          done: [],
          failed: [],
          waiting_at: "review",
          started_at: "",
          gate: {
            gate_id: "review",
            workflow: DETAIL.id,
            reviewing: '{"risk": "low"}',
            rejection_reruns: "compose",
          },
        },
      ],
    });
    let decidedOnSess1 = false;
    let approved: boolean | null = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, async ({ request }) => {
        decidedOnSess1 = true;
        const body = (await request.json()) as { approve: boolean };
        approved = body.approve;
        return HttpResponse.json({ ok: true, approved: body.approve });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    expect(await screen.findByText('{"risk": "low"}')).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approved).toBe(true));
    expect(decidedOnSess1).toBe(true);
  });

  it("rejects with feedback through the gate panel, posting the typed text session-scoped", async () => {
    mountApi({
      runs: [
        {
          session_id: "sess-1",
          workflow_id: DETAIL.id,
          run_title: "",
          state: "waiting",
          active: [],
          done: [],
          failed: [],
          waiting_at: "review",
          started_at: "",
          gate: {
            gate_id: "review",
            workflow: DETAIL.id,
            reviewing: '{"risk": "low"}',
            rejection_reruns: "compose",
          },
        },
      ],
    });
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true, approved: false });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    await screen.findByText('{"risk": "low"}');
    await userEvent.click(screen.getByRole("button", { name: "Reject with feedback" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "too risky");
    await userEvent.click(screen.getByRole("button", { name: "Send rejection" }));

    await waitFor(() => expect(posted).toEqual({ approve: false, feedback: "too risky" }));
  });

  it("does not show a gate that belongs to a different session", async () => {
    mountApi({
      runs: [
        {
          session_id: "sess-9",
          workflow_id: DETAIL.id,
          run_title: "",
          state: "waiting",
          active: [],
          done: [],
          failed: [],
          waiting_at: "review",
          started_at: "",
          gate: {
            gate_id: "review",
            workflow: DETAIL.id,
            reviewing: '{"risk": "low"}',
            rejection_reruns: "compose",
          },
        },
      ],
      runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id } },
    });
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    await screen.findByRole("button", { name: "Pause" });
    expect(screen.queryByText('{"risk": "low"}')).not.toBeInTheDocument();
  });

  it("renders the run's result once it lands for the acting session", async () => {
    mountApi({
      runStates: { "sess-1": { ...IDLE, workflow: DETAIL.id, result: "wrote docs/adr-014.md" } },
    });
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    expect(await screen.findByText(/wrote docs\/adr-014\.md/)).toBeInTheDocument();
  });

  it("offers Stop whenever the acting session's run is running, and asks before posting it", async () => {
    mountApi({ runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id } } });
    let stoppedOnSess1 = false;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/stop`, () => {
        stoppedOnSess1 = true;
        return HttpResponse.json({ ok: true, stopped: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(stoppedOnSess1).toBe(false);
    expect(screen.getByText("Stop for good?")).toBeInTheDocument();

    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(stoppedOnSess1).toBe(true));
  });

  it("cancelling the Stop confirm posts nothing", async () => {
    mountApi({ runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id } } });
    let stopPosted = false;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/stop`, () => {
        stopPosted = true;
        return HttpResponse.json({ ok: true, stopped: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    await userEvent.click(await screen.findByRole("button", { name: "Stop" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(stopPosted).toBe(false);
  });

  it("offers no Schedule it button — the workflow page has not wired one yet", async () => {
    mountApi();
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await screen.findByRole("button", { name: "Run" });
    expect(screen.queryByRole("button", { name: "Schedule it" })).not.toBeInTheDocument();
  });

  it("renders the automode switch on purely from the run-state poll, no click involved", async () => {
    mountApi({
      runStates: { "sess-1": { ...IDLE, running: true, workflow: DETAIL.id, auto_approve: true } },
    });
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    const switchInput = await screen.findByRole("checkbox", { name: /Auto approve gates/ });
    expect(switchInput).toBeChecked();
  });

  it("flips automode on the acting session's run, posting it session-scoped, and the switch follows the poll", async () => {
    // The run/state fixture is a shared, mutable object: the automode POST below mutates it
    // in place, so the very next poll -- not a locally remembered value -- is what the switch
    // renders from, the same way the live backend's own flag would round-trip.
    const runStates: Record<string, RunState> = {
      "sess-1": { ...IDLE, running: true, workflow: DETAIL.id, auto_approve: false },
    };
    mountApi({ runStates });
    let postedOnSess1: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/automode`, async ({ request }) => {
        const body = (await request.json()) as { on: boolean };
        postedOnSess1 = body;
        runStates["sess-1"] = { ...runStates["sess-1"], auto_approve: body.on };
        return HttpResponse.json({ ok: true, auto_approve: body.on });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    const switchInput = await screen.findByRole("checkbox", { name: /Auto approve gates/ });
    expect(switchInput).not.toBeChecked();

    await userEvent.click(switchInput);

    await waitFor(() => expect(postedOnSess1).toEqual({ on: true }));
    await waitFor(() => expect(switchInput).toBeChecked());
  });

  it("takes the confirm away while a start is in flight, so a second press is not a second run", async () => {
    mountApi();
    let release = (): void => {};
    const held = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    const counted = mountLaunch(() => HttpResponse.json({ ok: true, running: DETAIL.id }));
    server.use(
      http.post(`${BASE}/api/sessions/sess-new/run`, async () => {
        counted.starts += 1;
        await held;
        return HttpResponse.json({ ok: true, running: DETAIL.id });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(await within(dialog).findByLabelText("context"), "a decision");
    const runIt = within(dialog).getByRole("button", { name: "Run it" });
    fireEvent.click(runIt);
    await waitFor(() => expect(runIt).toBeDisabled());
    fireEvent.click(runIt);

    await waitFor(() => expect(counted.starts).toBe(1));
    release();
    await waitFor(() => expect(counted.starts).toBe(1));
  });

  it("leaves a start that got no answer unknown, keeps the values, and sends nothing again", async () => {
    mountApi();
    const counted = mountLaunch(() => HttpResponse.error());
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a decision");

    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByText(/This run may or may not have started/),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("context")).toHaveValue("a decision");
    expect(within(dialog).getByRole("button", { name: "Start it again" })).toBeEnabled();
    expect(counted.starts).toBe(1);
  });

  it("settles an unknown start by reading the runs, and acts on the run it finds", async () => {
    const running: RunView = {
      session_id: "sess-new",
      workflow_id: DETAIL.id,
      run_title: "Check against the literature, a decision",
      state: "running",
      active: [],
      done: [],
      failed: [],
      waiting_at: null,
      gate: null,
      started_at: "2026-09-16T09:00:00Z",
    };
    mountApi({
      runs: [running],
      runStates: { "sess-new": { ...IDLE, running: true, workflow: DETAIL.id } },
    });
    const counted = mountLaunch(() => HttpResponse.error());
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a decision");
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(
      await within(dialog).findByRole("button", { name: "Check whether it started" }),
    );

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(counted.starts).toBe(1);
  });

  it("says nothing started inside the open setup, with the second start there to press", async () => {
    mountApi({ recent: [endedRun({ session_id: "somebody-else" })] });
    const counted = mountLaunch(() => HttpResponse.error());
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await setUpRun("a decision");
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(
      await within(dialog).findByRole("button", { name: "Check whether it started" }),
    );

    // Inside the dialog, never behind it: the setup is still open over an aria-hidden page, so an
    // answer rendered anywhere else is an answer the person who asked for it cannot read.
    expect(await within(dialog).findByText(/No run was started/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/may or may not have started/)).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("context")).toHaveValue("a decision");
    expect(within(dialog).getByRole("button", { name: "Start it again" })).toBeEnabled();
    expect(counted.starts).toBe(1);
  });

  it("reruns on the previous run's own values, and names the run after the new one", async () => {
    mountApi({ history: [endedRun()] });
    const posted = capturedLaunch();
    render(<RunControls detail={PRESENTED} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run again" }));
    const dialog = await screen.findByRole("dialog");
    const field = await within(dialog).findByLabelText("context");
    expect(field).toHaveValue("the old context");

    await userEvent.clear(field);
    await userEvent.type(field, "a newer context");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    // The previous run's name was composed from its own input, so it follows the new one rather
    // than describing an answer this run is not being given.
    await waitFor(() =>
      expect(posted.body).toEqual({
        workflow_id: DETAIL.id,
        arguments: { context: "a newer context" },
        run_title: "Check against the literature, a newer context",
      }),
    );
  });

  it("keeps a run name somebody wrote themselves, whatever the new input says", async () => {
    mountApi({ history: [endedRun({ run_title: "Monday literature check" })] });
    const posted = capturedLaunch();
    render(<RunControls detail={PRESENTED} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run again" }));
    const dialog = await screen.findByRole("dialog");
    const field = await within(dialog).findByLabelText("context");
    await userEvent.clear(field);
    await userEvent.type(field, "a newer context");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() =>
      expect(posted.body).toEqual({
        workflow_id: DETAIL.id,
        arguments: { context: "a newer context" },
        run_title: "Monday literature check",
      }),
    );
  });

  it("reruns the run the reader has open, never the newest one the history holds", async () => {
    mountApi({ history: [NEWEST_RUN, endedRun()] });
    render(<RunControls detail={PRESENTED} sessionId={null} viewedRunId="run-4" />);

    await userEvent.click(await screen.findByRole("button", { name: "Run again" }));
    const dialog = await screen.findByRole("dialog");

    expect(await within(dialog).findByLabelText("context")).toHaveValue("the old context");
  });

  it("cites the run the reader has open when it makes the next version", async () => {
    mountApi({ history: [NEWEST_RUN, endedRun()] });
    let adapted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-draft" }) }),
      ),
      http.post(`${BASE}/api/sessions/sess-draft/adapt`, async ({ request }) => {
        adapted = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    render(<RunControls detail={PRESENTED} sessionId={null} viewedRunId="run-4" />);

    await userEvent.click(await screen.findByRole("button", { name: "Make a new version" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/Adapting from Check against the literature, the old context/),
    ).toBeInTheDocument();
    const does = within(dialog).getByLabelText("What this version does");
    await userEvent.clear(does);
    await userEvent.type(does, "Check a decision, and name the sections it could not read.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Make the new version" }));

    await waitFor(() =>
      expect(adapted).toEqual({
        workflow_id: DETAIL.id,
        ops: [
          { op: "set_description", to: "Check a decision, and name the sections it could not read." },
        ],
        source_run_id: "run-4",
      }),
    );
  });

  it("takes back the session it opened when the adaptation is refused", async () => {
    mountApi({ history: [endedRun()] });
    const removed: string[] = [];
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-draft" }) }),
      ),
      http.post(`${BASE}/api/sessions/sess-draft/adapt`, () =>
        HttpResponse.json({ ok: false, detail: "no run 'run-4' is archived here" }, { status: 404 }),
      ),
      http.delete(`${BASE}/api/sessions/:id`, ({ params }) => {
        removed.push(paramId(params.id));
        return new HttpResponse(null, { status: 204 });
      }),
    );
    render(<RunControls detail={PRESENTED} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Make a new version" }));
    const dialog = await screen.findByRole("dialog");
    const does = within(dialog).getByLabelText("What this version does");
    await userEvent.clear(does);
    await userEvent.type(does, "Check a decision, and name the sections it could not read.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Make the new version" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("no run");
    await waitFor(() => expect(removed).toEqual(["sess-draft"]));
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("opens on the gate mode this deployment names as its own default", async () => {
    mountApi({ limits: { ...BRAKES, default_gate_mode: "auto" } });
    const posted = capturedLaunch();
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByRole("radio", { name: "Pass automatically" })).toBeChecked(),
    );
    await userEvent.type(await within(dialog).findByLabelText("context"), "a payments system");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() =>
      expect(posted.body).toEqual({
        workflow_id: DETAIL.id,
        arguments: { context: "a payments system" },
        gate_mode: "auto",
        run_title: "Check against the literature",
      }),
    );
  });

  it("reads what a run is held to under the key the rest of the app shares", async () => {
    mountApi();
    const { queryClient } = render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));

    await waitFor(() => expect(queryClient.getQueryData(studioKeys.runLimits)).toBeDefined());
  });

  it("states the money brakes this deployment holds a run to, before it is started", async () => {
    mountApi();
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");

    expect(await within(dialog).findByText("$2 per run")).toBeInTheDocument();
    expect(within(dialog).getByText("$25 per day, warned at 80%")).toBeInTheDocument();
  });

  it("says no limit is configured rather than naming a ceiling of zero", async () => {
    mountApi({ limits: { ...BRAKES, brakes: [] } });
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");

    expect(await within(dialog).findByText(/No limit configured/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/\$0/)).not.toBeInTheDocument();
  });

  it("makes a new version out of the last run, citing it, and opens the draft", async () => {
    mountApi({ history: [endedRun()] });
    let adapted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ session: sessionFixture({ id: "sess-draft" }) }),
      ),
      http.post(`${BASE}/api/sessions/sess-draft/adapt`, async ({ request }) => {
        adapted = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    render(<RunControls detail={DETAIL} sessionId={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "Make a new version" }));
    const dialog = await screen.findByRole("dialog");
    const does = within(dialog).getByLabelText("What this version does");
    await userEvent.clear(does);
    await userEvent.type(does, "Check a decision, and say when the reading is too thin.");
    await userEvent.type(within(dialog).getByLabelText("Why"), "It claimed more than it read.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Make the new version" }));

    await waitFor(() =>
      expect(adapted).toEqual({
        workflow_id: DETAIL.id,
        ops: [
          { op: "set_description", to: "Check a decision, and say when the reading is too thin." },
        ],
        source_run_id: "run-4",
        feedback: "It claimed more than it read.",
      }),
    );
    await waitFor(() =>
      expect(routerPush).toHaveBeenCalledWith("/workflows/draft?session=sess-draft"),
    );
  });

  it("reads its run state under the workflow's own run key", async () => {
    mountApi();
    const { queryClient } = render(<RunControls detail={DETAIL} sessionId="sess-1" />);

    await waitFor(() =>
      expect(queryClient.getQueryState(runKeys.state(DETAIL.id, "sess-1"))).toBeDefined(),
    );
  });
});
