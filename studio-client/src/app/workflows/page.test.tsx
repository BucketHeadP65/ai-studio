import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { RunView, SessionSummary } from "@/api/sessions-types";
import type { RunEnvelope, WorkflowGraph } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the board's cards see the mocked module.
import WorkflowsPage from "./page";

// The fleet board's own network surface: every request its mount can fire is mocked here (MSW
// errors on anything unhandled). `/api/runs` (the live pool) drives waiting/running/paused,
// `/api/runs/recent` (the archive) drives the tail of the stack, and `/api/workflow/:id` answers
// one card per distinct workflow id on the board, which supplies both the graph and the title an
// unnamed run falls back to. `/api/sessions` is still answered here and nothing on the board reads
// it: a run is named by itself, never by the session it happens to run in.

const BASE = "http://studio.test";

function runViewFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "sess-1",
    workflow_id: "wf.lit-check.v5",
    state: "running",
    active: ["draft"],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-25T09:00:00Z",
    run_title: "",
    ...overrides,
  };
}

function runEnvelopeFixture(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    spend: null,
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-1",
    workflow_id: "wf.metric-sweep.v1",
    outcome: "completed",
    failure: "",
    started_at: "2026-08-24T09:00:00Z",
    ended_at: "2026-08-24T09:05:00Z",
    runner: "app:harness",
    session_id: "sess-9",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    run_title: "",
    ...overrides,
  };
}

function sessionFixture(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: "sess-1",
    project_id: "",
    title: "",
    started_at: "2026-08-25T09:00:00Z",
    last_activity_at: "2026-08-25T09:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "runner",
    cost_usd: "0",
    unpriced: 0,
    context: null,
    ...overrides,
  };
}

const emptyGraph: WorkflowGraph = { name: "wf", version: 1, nodes: [], edges: [] };

function mountApi(overrides: {
  runs?: RunView[];
  recent?: RunEnvelope[];
  sessions?: SessionSummary[];
  runsStatus?: number;
  cardTitle?: string;
}) {
  server.use(
    http.get(`${BASE}/api/runs`, () => {
      if (overrides.runsStatus) {
        return HttpResponse.json(
          { detail: "the run pool is unreachable" },
          { status: overrides.runsStatus },
        );
      }
      return HttpResponse.json({ runs: overrides.runs ?? [] });
    }),
    http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: overrides.recent ?? [] })),
    http.get(`${BASE}/api/sessions`, () =>
      HttpResponse.json({ sessions: overrides.sessions ?? [] }),
    ),
    http.get(`${BASE}/api/workflow/:id`, ({ params }) =>
      HttpResponse.json({
        summary: "",
        applicability: "",
        archived_at: null,
        ok: true,
        id: params.id,
        name: params.id,
        title: overrides.cardTitle ?? "",
        version: 1,
        description: "",
        graph: emptyGraph,
        issues: [],
        fits: true,
        missing: [],
        brings: [],
        spec_yaml: "",
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
      }),
    ),
  );
}

describe("WorkflowsPage ordering", () => {
  it("stacks cards waiting first, then running, then paused, then the archive", async () => {
    mountApi({
      runs: [
        runViewFixture({ session_id: "s-run", workflow_id: "wf.run", state: "running" }),
        runViewFixture({
          run_title: "",
          started_at: "",
          session_id: "s-wait",
          workflow_id: "wf.wait",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.wait",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
        runViewFixture({ session_id: "s-pause", workflow_id: "wf.pause", state: "paused" }),
      ],
      recent: [runEnvelopeFixture({ run_id: "r-1", workflow_id: "wf.archived" })],
      sessions: [
        sessionFixture({ id: "s-run", title: "Run session" }),
        sessionFixture({ id: "s-wait", title: "Wait session" }),
        sessionFixture({ id: "s-pause", title: "Pause session" }),
        sessionFixture({ id: "sess-9", title: "Archived session" }),
      ],
    });
    render(<WorkflowsPage />);

    await screen.findAllByText("wf.wait");
    const items = screen.getAllByRole("listitem");
    const tags = items.map(
      (item) => within(item).getByText(/^wf\./, { selector: ".font-mono" }).textContent,
    );
    expect(tags).toEqual(["wf.wait", "wf.run", "wf.pause", "wf.archived"]);
  });
});

describe("WorkflowsPage summary sentence", () => {
  it("says nothing is happening when every count is zero", async () => {
    mountApi({});
    render(<WorkflowsPage />);

    await screen.findByText("Nothing running.");
  });

  it("names each non-zero count and omits the rest", async () => {
    mountApi({
      runs: [
        runViewFixture({
          run_title: "",
          started_at: "",
          session_id: "s-wait-1",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.lit-check.v5",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
        runViewFixture({
          run_title: "",
          started_at: "",
          session_id: "s-wait-2",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.lit-check.v5",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
      ],
      recent: [],
    });
    render(<WorkflowsPage />);

    await screen.findByText("2 waiting on you.");
  });

  it("joins the waiting, running, and finished-today counts into one sentence", async () => {
    const today = new Date().toISOString();
    mountApi({
      runs: [
        runViewFixture({
          run_title: "",
          started_at: "",
          session_id: "s-wait",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.lit-check.v5",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
        runViewFixture({ session_id: "s-run", state: "running" }),
      ],
      recent: [
        runEnvelopeFixture({ run_id: "r-1", ended_at: today }),
        runEnvelopeFixture({ run_id: "r-2", ended_at: today }),
      ],
    });
    render(<WorkflowsPage />);

    await screen.findByText("1 waiting on you, 1 running, 2 finished today.");
  });
});

describe("WorkflowsPage run names", () => {
  it("names a card by the run's own title, live and archived alike", async () => {
    mountApi({
      runs: [
        runViewFixture({
          session_id: "sess-1",
          workflow_id: "wf.titled",
          state: "running",
          run_title: "Which cache do we use?",
        }),
      ],
      recent: [
        runEnvelopeFixture({
          run_id: "r-1",
          workflow_id: "wf.archived",
          run_title: "Which queue do we drain?",
        }),
      ],
    });
    render(<WorkflowsPage />);

    expect(await screen.findByRole("link", { name: "Which cache do we use?" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Which queue do we drain?" })).toBeInTheDocument();
  });

  it("falls back to the workflow card's own title for a run nobody named", async () => {
    mountApi({ cardTitle: "Check against the literature" });
    server.use(
      http.get(`${BASE}/api/runs`, () =>
        HttpResponse.json({
          runs: [runViewFixture({ workflow_id: "wf.untitled", run_title: "" })],
        }),
      ),
    );
    render(<WorkflowsPage />);

    expect(
      await screen.findByRole("link", { name: "Check against the literature" }),
    ).toBeInTheDocument();
  });

  it("falls back to the workflow id when neither the run nor its card carries a name", async () => {
    mountApi({ runs: [runViewFixture({ workflow_id: "wf.untitled", run_title: "" })] });
    render(<WorkflowsPage />);

    expect(await screen.findByRole("link", { name: "wf.untitled" })).toBeInTheDocument();
  });

  it("never names a run after the session it happens to run in", async () => {
    mountApi({
      runs: [
        runViewFixture({ session_id: "sess-titled", workflow_id: "wf.untitled", run_title: "" }),
      ],
      sessions: [sessionFixture({ id: "sess-titled", title: "a chat about caches" })],
    });
    render(<WorkflowsPage />);

    expect(await screen.findByRole("link", { name: "wf.untitled" })).toBeInTheDocument();
    expect(screen.queryByText("a chat about caches")).not.toBeInTheDocument();
  });
});

describe("WorkflowsPage acts", () => {
  it("decides a gate from its card without navigating away from the board", async () => {
    mountApi({
      runs: [
        runViewFixture({
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.lit-check.v5",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
      ],
    });
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/decide`, () =>
        HttpResponse.json({ ok: true, approved: true }),
      ),
    );
    render(<WorkflowsPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Fleet" })).toBeInTheDocument();
  });

  it("resumes a paused run from its card", async () => {
    mountApi({ runs: [runViewFixture({ state: "paused" })] });
    server.use(
      http.post(`${BASE}/api/sessions/sess-1/run/resume`, () =>
        HttpResponse.json({ ok: true, running: "wf.lit-check.v5" }),
      ),
    );
    render(<WorkflowsPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Fleet" })).toBeInTheDocument();
  });

  it("toggles a recent run's pin, reading back the flip from a stateful refetch", async () => {
    const recent = [runEnvelopeFixture({ run_id: "run-9", pinned: false })];
    server.use(
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/runs/recent`, () => HttpResponse.json({ runs: recent })),
      http.get(`${BASE}/api/sessions`, () => HttpResponse.json({ sessions: [] })),
      http.get(`${BASE}/api/workflow/:id`, ({ params }) =>
        HttpResponse.json({
          title: "",
          summary: "",
          applicability: "",
          archived_at: null,
          ok: true,
          id: params.id,
          name: params.id,
          version: 1,
          description: "",
          graph: emptyGraph,
          issues: [],
          fits: true,
          missing: [],
          brings: [],
          spec_yaml: "",
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
        }),
      ),
      http.patch(`${BASE}/api/runs/history/run-9`, () => {
        recent[0] = { ...recent[0], pinned: true };
        return HttpResponse.json({ ok: true, pinned: true });
      }),
    );
    render(<WorkflowsPage />);

    const pin = await screen.findByRole("button", { name: "Pin this run" });
    await userEvent.click(pin);

    expect(await screen.findByRole("button", { name: "Unpin this run" })).toBeInTheDocument();
  });

  it("routes to the workflow library when Start a run is clicked", async () => {
    mountApi({});
    render(<WorkflowsPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Start a run" }));

    expect(routerPush).toHaveBeenCalledWith("/workflows/library");
  });
});

describe("WorkflowsPage poll errors", () => {
  it("shows an alert when the live pool cannot be reached", async () => {
    mountApi({ runsStatus: 500 });
    render(<WorkflowsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("unreachable");
  });
});
