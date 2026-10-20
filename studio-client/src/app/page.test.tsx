import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { RunView } from "@/api/sessions-types";
import type { GraphNode, LibraryEntry, RunEnvelope, WorkflowGraph } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the cards see the mocked module.
import HomePage from "./page";

// Home's whole network surface: the live pool, the saved workflows, and the newest ended runs.
// MSW errors on anything else, so a request this screen should not be making fails the test rather
// than reaching a backend. Setting a workflow up adds that workflow's own detail and the three
// calls a launch makes, and nothing here fetches a graph for a card.

const BASE = "http://studio.test";

// The words this generic screen must never say: they belong to one knowledge base's own schema,
// never to the app that renders any of them.
const BORROWED_VOCABULARY =
  /\b(paper|papers|literature|citation|corpus|note|notes|digest|finding|source|zotero)\b/i;
const PLAIN_PUNCTUATION = /^[A-Za-z0-9 ,.?()]*$/;

function runViewFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "sess-1",
    workflow_id: "wf.weekly-sweep.v2",
    run_title: "",
    state: "running",
    active: ["compose"],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-09-10T09:00:00Z",
    ...overrides,
  };
}

function runEnvelopeFixture(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    spend: null,
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-1",
    workflow_id: "wf.weekly-sweep.v2",
    run_title: "",
    outcome: "completed",
    failure: "",
    started_at: "2026-09-09T09:00:00Z",
    ended_at: "2026-09-09T09:05:00Z",
    runner: "app:harness",
    session_id: "sess-9",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    ...overrides,
  };
}

function libraryEntryFixture(overrides: Partial<LibraryEntry> = {}): LibraryEntry {
  return {
    id: "wf.weekly-sweep.v2",
    title: "Weekly sweep",
    summary: "Checks the queue every week and writes up what changed.",
    provisional: false,
    trust: {
      level: "unverified",
      runs_with_evidence: 0,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    needs: [],
    missing: [],
    brings: [],
    version: 2,
    archived_at: null,
    shape: { steps: 2, person_gates: 1, agent_gates: 0 },
    runs: { last: null },
    ...overrides,
  };
}

// The one section named by its own marker, narrowed rather than cast: a missing section is a
// failure here, not an `as`.
function sectionOf(container: HTMLElement, id: string): HTMLElement {
  const node = container.querySelector(`[data-section="${id}"]`);
  if (!(node instanceof HTMLElement)) throw new Error(`Home has no ${id} section`);
  return node;
}

function paramNode(id: string, overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    key: id,
    id,
    kind: "param",
    signature: "",
    detail: "",
    tools: [],
    is_output: false,
    unknown: false,
    brought: false,
    briefing: "",
    input_schemas: {},
    output_schema: { type: "string" },
    source: "",
    required: true,
    turns: null,
    ...overrides,
  };
}

function gateNode(id: string): GraphNode {
  return { ...paramNode(id), kind: "gate", output_schema: null, required: false };
}

interface MountOptions {
  runs?: RunView[];
  recent?: RunEnvelope[];
  library?: LibraryEntry[];
  runsStatus?: number;
  recentStatus?: number;
  libraryStatus?: number;
  graph?: WorkflowGraph;
}

function workflowDetail(id: string, graph: WorkflowGraph) {
  return {
    archived_at: null,
    ok: true,
    id,
    name: id,
    version: 2,
    description: "",
    title: "Weekly sweep",
    summary: "Checks the queue every week and writes up what changed.",
    applicability: "",
    provisional: false,
    graph,
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
    presentation: {
      purpose: "Keeps the queue moving.",
      produces: "A short write up.",
      owner_participation: "You approve the write up.",
      not_for: "Anything that must land the same hour.",
      inputs: [{ key: "topic", label: "What to sweep", control: "text" }],
      result_fields: [],
      actions: [],
    },
  };
}

const DEFAULT_GRAPH: WorkflowGraph = {
  name: "wf.weekly-sweep",
  version: 2,
  nodes: [paramNode("topic")],
  edges: [],
};

function mountApi(options: MountOptions = {}) {
  const graph = options.graph ?? DEFAULT_GRAPH;
  server.use(
    http.get(`${BASE}/api/runs`, () =>
      options.runsStatus
        ? HttpResponse.json({ detail: "the run pool is unreachable" }, { status: options.runsStatus })
        : HttpResponse.json({ runs: options.runs ?? [] }),
    ),
    http.get(`${BASE}/api/runs/recent`, () =>
      options.recentStatus
        ? HttpResponse.json({ detail: "the archive is unreachable" }, { status: options.recentStatus })
        : HttpResponse.json({ runs: options.recent ?? [] }),
    ),
    http.get(`${BASE}/api/library`, () =>
      options.libraryStatus
        ? HttpResponse.json({ detail: "the library is unreachable" }, { status: options.libraryStatus })
        : HttpResponse.json(options.library ?? []),
    ),
    http.get(`${BASE}/api/workflow/:id`, ({ params }) =>
      HttpResponse.json(workflowDetail(String(params.id), graph)),
    ),
  );
}

describe("Home names itself and leads with starting an automation", () => {
  it("carries one H1 and its four sections in order", async () => {
    mountApi({ library: [libraryEntryFixture()] });
    const { container } = render(<HomePage />);

    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    const headings = Array.from(container.querySelectorAll("h1, h2, h3, h4")).map((node) => [
      node.tagName,
      node.textContent,
    ]);
    expect(headings).toEqual([
      ["H1", "Home"],
      ["H2", "Start automation"],
      ["H2", "Active automations"],
      ["H2", "Needs your decision"],
      ["H2", "Recent results"],
    ]);
  });

  it("offers Create workflow as a link to the guided route, and keeps chat reachable", async () => {
    mountApi({});
    render(<HomePage />);

    const create = await screen.findByRole("link", { name: "Create workflow" });
    expect(create).toHaveAttribute("href", "/workflows/draft");
    expect(screen.getByRole("link", { name: "Open chat" })).toHaveAttribute("href", "/chat");
  });

  it("lists a saved workflow with its own words and one act on it", async () => {
    mountApi({ library: [libraryEntryFixture()] });
    render(<HomePage />);

    expect(await screen.findByRole("link", { name: "Weekly sweep" })).toHaveAttribute(
      "href",
      "/workflows/library/wf.weekly-sweep.v2",
    );
    expect(
      screen.getByText("Checks the queue every week and writes up what changed."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up Weekly sweep" })).toBeInTheDocument();
  });
});

describe("Home orders what is happening", () => {
  it("shows an active run, and a waiting decision ahead of the recent results", async () => {
    mountApi({
      runs: [
        runViewFixture({ session_id: "s-live", run_title: "This week's sweep", state: "running" }),
        runViewFixture({
          started_at: "",
          session_id: "s-gate",
          run_title: "Last week's sweep",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.weekly-sweep.v2",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
      ],
      recent: [runEnvelopeFixture({ run_title: "The sweep before that" })],
    });
    const { container } = render(<HomePage />);

    await screen.findByText("This week's sweep");

    const active = sectionOf(container, "home-active");
    const decisions = sectionOf(container, "home-decisions");
    const results = sectionOf(container, "home-results");
    expect(within(active).getByText("This week's sweep")).toBeInTheDocument();
    expect(within(decisions).getByText("Last week's sweep")).toBeInTheDocument();
    expect(within(results).getByText("The sweep before that")).toBeInTheDocument();

    // The decision comes before the result in the document, so a reader meets what is asked of
    // them before what is already finished.
    expect(
      decisions.compareDocumentPosition(results) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("names a run nobody named after its saved workflow, never after its session", async () => {
    mountApi({
      runs: [runViewFixture({ session_id: "s-live", run_title: "" })],
      library: [libraryEntryFixture()],
    });
    const { container } = render(<HomePage />);

    const active = await screen.findByText("Weekly sweep", {
      selector: '[data-section="home-active"] a',
    });
    expect(active).toBeInTheDocument();
    expect(container.textContent).not.toContain("sess-1");
  });

  it("says who is waiting and who is running in one restrained live region", async () => {
    mountApi({
      runs: [
        runViewFixture({ session_id: "s-live", state: "running" }),
        runViewFixture({
          run_title: "",
          started_at: "",
          session_id: "s-gate",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.weekly-sweep.v2",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
      ],
    });
    const { container } = render(<HomePage />);

    await screen.findByText("1 waiting on you, 1 running.");
    const live = container.querySelectorAll("[aria-live]");
    expect(live).toHaveLength(1);
    expect(live[0].getAttribute("aria-live")).toBe("polite");
  });
});

describe("Home says what an empty shelf is for", () => {
  it("writes an act into every empty section", async () => {
    mountApi({});
    render(<HomePage />);

    expect(await screen.findByText("No saved workflows yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Create one and it lands here, ready to set up and run."),
    ).toBeInTheDocument();
    expect(screen.getByText("Nothing is running.")).toBeInTheDocument();
    expect(
      screen.getByText("Set up a saved workflow above and it shows here while it works."),
    ).toBeInTheDocument();
    expect(screen.getByText("No decision is waiting.")).toBeInTheDocument();
    expect(
      screen.getByText("When a run reaches a gate it stops here until you answer it."),
    ).toBeInTheDocument();
    expect(screen.getByText("No results yet.")).toBeInTheDocument();
    expect(screen.getByText("A run that ends lands here with what it produced.")).toBeInTheDocument();
  });

  it("keeps every string it writes to plain punctuation and borrows no vocabulary", async () => {
    mountApi({
      library: [libraryEntryFixture({ summary: "" })],
      runs: [
        runViewFixture({ session_id: "s-live", run_title: "The weekly sweep", state: "running" }),
        runViewFixture({ session_id: "s-hold", run_title: "The held sweep", state: "paused" }),
        runViewFixture({
          started_at: "",
          session_id: "s-gate",
          run_title: "The sweep before it",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.weekly-sweep.v2",
            reviewing: "two sections, one thin",
            rejection_reruns: "compose",
          },
        }),
      ],
      recent: [runEnvelopeFixture({ run_title: "The sweep before that" })],
    });
    const { container } = render(<HomePage />);
    await screen.findByText("The sweep before that");

    // Everything a reader can read except what the backend supplied. Machine output is set in mono,
    // and the values these fixtures stand in for (ids, run titles, a reviewed value) are the
    // backend's words, not this screen's.
    const supplied = new Set([
      "wf.weekly-sweep.v2",
      "Weekly sweep",
      "The weekly sweep",
      "The held sweep",
      "The sweep before it",
      "The sweep before that",
      "two sections, one thin",
    ]);
    const strings = new Set<string>();
    for (const node of container.querySelectorAll("*")) {
      const label = node.getAttribute("aria-label");
      if (label) strings.add(label);
      if (node.classList.contains("font-mono")) continue;
      if (node.children.length > 0 || !node.textContent) continue;
      if (supplied.has(node.textContent.trim())) continue;
      strings.add(node.textContent);
    }
    for (const text of strings) {
      expect(text).toMatch(PLAIN_PUNCTUATION);
      expect(text).not.toMatch(BORROWED_VOCABULARY);
    }
  });
});

describe("Home never reads an unread pool as an empty one", () => {
  it("alerts in both live sections and claims neither count when the pool refuses", async () => {
    mountApi({ runsStatus: 503 });
    const { container } = render(<HomePage />);

    const active = sectionOf(container, "home-active");
    const decisions = sectionOf(container, "home-decisions");
    expect(await within(active).findByRole("alert")).toHaveTextContent(
      "Could not read the runs in flight.",
    );
    expect(within(decisions).getByRole("alert")).toHaveTextContent(
      "Could not read what is waiting on you.",
    );

    // The empty copy is an answer, and nothing answered.
    expect(within(active).queryByText("Nothing is running.")).not.toBeInTheDocument();
    expect(within(decisions).queryByText("No decision is waiting.")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing is running right now.")).not.toBeInTheDocument();
    expect(screen.getByText("Could not read what is running.")).toBeInTheDocument();
  });

  it("writes the empty copy again once the pool answers with nothing", async () => {
    mountApi({});
    const { container } = render(<HomePage />);

    expect(await screen.findByText("Nothing is running right now.")).toBeInTheDocument();
    expect(within(sectionOf(container, "home-decisions")).getByText("No decision is waiting."))
      .toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("Home announces a read it could not make", () => {
  it("alerts on the live pool, the library, and the archive, each in its own section", async () => {
    mountApi({ runsStatus: 503, recentStatus: 500, libraryStatus: 502 });
    const { container } = render(<HomePage />);

    const alerts = await screen.findAllByRole("alert");
    expect(alerts.map((alert) => alert.textContent?.split(".")[0])).toEqual([
      "Could not read the saved workflows",
      "Could not read the runs in flight",
      "Could not read what is waiting on you",
      "Could not read the recent results",
    ]);
    expect(
      within(sectionOf(container, "home-results")).getByRole("alert"),
    ).toBeInTheDocument();
  });
});

describe("Home keeps the advanced run controls out of sight", () => {
  it("offers no gate policy and no scheduling until a run is being set up", async () => {
    mountApi({ library: [libraryEntryFixture()], graph: { ...DEFAULT_GRAPH, nodes: [paramNode("topic"), gateNode("review")] } });
    render(<HomePage />);

    await screen.findByRole("button", { name: "Set up Weekly sweep" });
    expect(screen.queryByText("Gates on this run")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /schedule/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Set up Weekly sweep" }));
    expect(await screen.findByText("Gates on this run")).toBeInTheDocument();
  });
});

describe("Home sets a run up before it creates anything", () => {
  it("collects the inputs, names the run after the workflow, and stays on Home", async () => {
    mountApi({ library: [libraryEntryFixture()] });
    const created: string[] = [];
    const launched: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions`, () => {
        created.push("session");
        return HttpResponse.json({
          session: {
            id: "s-new",
            project_id: "",
            title: "",
            started_at: "2026-09-10T09:00:00Z",
            last_activity_at: "2026-09-10T09:00:00Z",
            archived: false,
            answering: false,
            turn_count: 0,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            kind: "runner",
            cost_usd: "0",
            unpriced: 0,
            context: null,
          },
        });
      }),
      http.patch(`${BASE}/api/sessions/s-new`, () =>
        HttpResponse.json({
          session: {
            id: "s-new",
            project_id: "",
            title: "Weekly sweep",
            started_at: "2026-09-10T09:00:00Z",
            last_activity_at: "2026-09-10T09:00:00Z",
            archived: false,
            answering: false,
            turn_count: 0,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            kind: "runner",
            cost_usd: "0",
            unpriced: 0,
            context: null,
          },
        }),
      ),
      http.post(`${BASE}/api/sessions/s-new/run`, async ({ request }) => {
        launched.push(await request.json());
        return HttpResponse.json({ ok: true, running: "wf.weekly-sweep.v2" });
      }),
    );
    render(<HomePage />);

    await userEvent.click(await screen.findByRole("button", { name: "Set up Weekly sweep" }));
    await screen.findByText("Set up this run");
    expect(created).toHaveLength(0);

    await userEvent.type(screen.getByLabelText("What to sweep"), "the open queue");
    await userEvent.click(screen.getByRole("button", { name: "Run it" }));

    await vi.waitFor(() => expect(launched).toHaveLength(1));
    expect(launched[0]).toEqual({
      workflow_id: "wf.weekly-sweep.v2",
      arguments: { topic: "the open queue" },
      run_title: "Weekly sweep, the open queue",
    });
    expect(routerPush).not.toHaveBeenCalled();
    expect(screen.queryByText("Set up this run")).not.toBeInTheDocument();
  });

  it("creates nothing when the setup is cancelled", async () => {
    mountApi({ library: [libraryEntryFixture()] });
    let created = 0;
    server.use(
      http.post(`${BASE}/api/sessions`, () => {
        created += 1;
        return HttpResponse.json({ session: null });
      }),
    );
    render(<HomePage />);

    await userEvent.click(await screen.findByRole("button", { name: "Set up Weekly sweep" }));
    await screen.findByText("Set up this run");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Set up this run")).not.toBeInTheDocument();
    expect(created).toBe(0);
  });
});

describe("Home answers a waiting gate where it is read", () => {
  it("sends the rejection with the words typed beside it", async () => {
    mountApi({
      runs: [
        runViewFixture({
          started_at: "",
          session_id: "s-gate",
          run_title: "Last week's sweep",
          state: "waiting",
          waiting_at: "review",
          gate: {
            gate_id: "review",
            workflow: "wf.weekly-sweep.v2",
            reviewing: "{}",
            rejection_reruns: "compose",
          },
        }),
      ],
    });
    const decided: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/sessions/s-gate/decide`, async ({ request }) => {
        decided.push(await request.json());
        return HttpResponse.json({ ok: true, approved: false });
      }),
    );
    render(<HomePage />);

    await userEvent.click(await screen.findByRole("button", { name: "Reject with feedback" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "the second section is thin");
    await userEvent.click(screen.getByRole("button", { name: "Send rejection" }));

    await vi.waitFor(() => expect(decided).toHaveLength(1));
    expect(decided[0]).toEqual({ approve: false, feedback: "the second section is thin" });
  });
});
