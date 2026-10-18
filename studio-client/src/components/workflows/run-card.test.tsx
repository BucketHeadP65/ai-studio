import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RunView } from "@/api/sessions-types";
import type { Outcome, RunEnvelope, WorkflowGraph } from "@/api/studio-types";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the card sees the mocked module.
import { RunCard } from "./run-card";

// The card's own anatomy: the top row's four facts, the gate panel appearing only on a live run
// actually waiting on a person, the archived explanation box for a machine-decided run, the one
// inline act each kind still owns, and the guarded whole-card click every kind shares.

afterEach(() => {
  routerPush.mockClear();
});

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
    spend: null,
    run_title: "",
    ...overrides,
  };
}

function runEnvelopeFixture(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    run_id: "run-1",
    workflow_id: "wf.lit-check.v5",
    outcome: "completed",
    failure: "",
    started_at: "2026-08-25T09:00:00Z",
    ended_at: "2026-08-25T09:05:00Z",
    runner: "app:harness",
    session_id: "sess-1",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    spend: null,
    run_title: "",
    ...overrides,
  };
}

const emptyGraph: WorkflowGraph = { name: "wf", version: 1, nodes: [], edges: [] };

describe("RunCard top row", () => {
  it("shows the run's own name, the workflow tag, the state pill, and the elapsed time", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture(),
            name: "the lit check",
            graph: emptyGraph,
            now: new Date("2026-08-25T09:01:30Z").getTime(),
          }}
        />
      </ul>,
    );

    expect(screen.getByRole("link", { name: "the lit check" })).toHaveAttribute(
      "href",
      "/workflows/wf.lit-check.v5",
    );
    expect(screen.getByText("wf.lit-check.v5")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("1m 30s")).toBeInTheDocument();
  });

  it("shows no elapsed time at all when started_at is unknown", () => {
    const { container } = render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture({ started_at: "" }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
          }}
        />
      </ul>,
    );

    expect(container.querySelector('[data-section="execution"]')).toHaveTextContent(
      "wf.lit-check.v5",
    );
    expect(screen.queryByText(/^\d+m /)).not.toBeInTheDocument();
  });

  it("reads an archived run's elapsed time as ended minus started, not against the clock", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({
              started_at: "2026-08-25T09:00:00Z",
              ended_at: "2026-08-25T09:05:00Z",
            }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByText("5m 0s")).toBeInTheDocument();
  });
});

describe("RunCard cost", () => {
  it("shows the run's own priced cost in the mono header row", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture({
              spend: {
                cost_usd: "0.0421",
                unpriced: 0,
                usage: {
                  input_tokens: 100,
                  output_tokens: 50,
                  cache_read_tokens: 0,
                  cache_write_tokens: 0,
                },
                steps: {},
              },
            }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
          }}
        />
      </ul>,
    );

    expect(screen.getByText("$0.0421")).toBeInTheDocument();
  });

  it("shows nothing extra when no ledger exists for the run", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture({ spend: null }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
          }}
        />
      </ul>,
    );

    expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
  });
});

describe("RunCard gate panel", () => {
  it("shows the gate panel only for a live run actually waiting on a person", () => {
    const onDecide = vi.fn();
    render(
      <ul>
        <RunCard
          card={{
            kind: "waiting",
            run: runViewFixture({
              state: "waiting",
              waiting_at: "review",
              gate: {
                gate_id: "review",
                workflow: "wf.lit-check.v5",
                reviewing: '{"title": "a draft"}',
                rejection_reruns: "compose",
              },
            }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
            onDecide,
            deciding: false,
          }}
        />
      </ul>,
    );

    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve.closest("a")).toBeNull();
  });

  it("shows no gate panel for a running card", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture(),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
          }}
        />
      </ul>,
    );

    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("shows no gate panel for a paused card", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "paused",
            run: runViewFixture({ state: "paused" }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
            onResume: vi.fn(),
            resuming: false,
          }}
        />
      </ul>,
    );

    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("shows the agent-decided box for an archived run whose gates the orchestrator decided", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ gates_orchestrated: ["quality-gate"] }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByText("Decided by the agent")).toBeInTheDocument();
    expect(
      screen.getByText(
        /quality-gate passed\. The orchestrator agent decided this, no person approved it\./,
      ),
    ).toBeInTheDocument();
  });

  it("shows the passed-automatically box for an archived run with an auto gate", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ gates_auto: ["size-gate"] }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByText("Passed automatically")).toBeInTheDocument();
  });

  it("shows no explanation box for an archived run with only person-approved gates", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ gates_approved: ["review"] }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.queryByText("Decided by the agent")).toBeNull();
    expect(screen.queryByText("Passed automatically")).toBeNull();
  });
});

describe("RunCard footer acts", () => {
  it("resumes a paused run without navigating away", async () => {
    const onResume = vi.fn();
    render(
      <ul>
        <RunCard
          card={{
            kind: "paused",
            run: runViewFixture({ state: "paused" }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
            onResume,
            resuming: false,
          }}
        />
      </ul>,
    );

    const resume = screen.getByRole("button", { name: "Resume" });
    expect(resume.closest("a")).toBeNull();

    await userEvent.click(resume);
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("keeps the act on a paused run above the execution facts, so a narrow card shows it first", () => {
    const { container } = render(
      <ul>
        <RunCard
          card={{
            kind: "paused",
            run: runViewFixture({ state: "paused" }),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
            onResume: vi.fn(),
            resuming: false,
          }}
        />
      </ul>,
    );

    const order = Array.from(container.querySelectorAll("[data-section]")).map((section) =>
      section.getAttribute("data-section"),
    );
    expect(order.indexOf("action")).toBeLessThan(order.indexOf("execution"));
    const action = container.querySelector('[data-section="action"]');
    expect(action).not.toBeNull();
    expect(action?.textContent).toBe("Resume");
  });

  it("names the pin in words beside its star, never by the star alone", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ pinned: true }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByRole("button", { name: "Unpin this run" })).toHaveTextContent("Pinned");
  });

  it("toggles a recent run's pin without navigating away, and names the button by its own state", async () => {
    const onTogglePin = vi.fn();
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ pinned: false }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin,
            pinning: false,
          }}
        />
      </ul>,
    );

    const pin = screen.getByRole("button", { name: "Pin this run" });
    expect(pin).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(pin);
    expect(onTogglePin).toHaveBeenCalledTimes(1);
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("carries the archived run's own provenance sentence in its footer", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ runner: "app:schedule:ingest-daily" }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    const sentence = screen.getByText("Started by a schedule, ingest-daily.");
    expect(sentence.closest("div")?.querySelector("svg")).not.toBeNull();
  });

  it("renders the same provenance chip, with the person icon, for a chat-started run", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ runner: "app:chat" }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    const sentence = screen.getByText("Started from chat.");
    expect(sentence.closest("div")?.querySelector("svg")).not.toBeNull();
  });
});

describe("RunCard whole-card click", () => {
  it("opens the workflow when the card body is clicked, live runs with no run id in the link", async () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "running",
            run: runViewFixture(),
            name: "the lit check",
            graph: emptyGraph,
            now: Date.now(),
          }}
        />
      </ul>,
    );

    await userEvent.click(screen.getByText("wf.lit-check.v5"));
    expect(routerPush).toHaveBeenCalledWith("/workflows/wf.lit-check.v5");
  });

  it("opens the workflow WITH the run id when an archived card body is clicked", async () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ run_id: "run-9" }),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    await userEvent.click(screen.getByText("wf.lit-check.v5"));
    expect(routerPush).toHaveBeenCalledWith("/workflows/wf.lit-check.v5?run=run-9");
  });

  it("never navigates when the click lands on the pin button", async () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture(),
            name: "the lit check",
            graph: emptyGraph,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Pin this run" }));
    expect(routerPush).not.toHaveBeenCalled();
  });
});

// The card's own reading order: what the run is and what it needs from a person first, then what
// it produced, then how it ran, and the graph last and folded away.

const GRAPH: WorkflowGraph = {
  name: "wf",
  version: 1,
  nodes: [
    {
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "draft() → str",
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
  ],
  edges: [],
};

function checked(overrides: Partial<Outcome> = {}): Outcome {
  return {
    status: "succeeded",
    summary: "it published",
    attempted: 2,
    succeeded: 2,
    skipped: 0,
    failed: 0,
    retry_attempts: 0,
    primary_input: 2,
    generation: 0,
    checks: [],
    artifacts: [
      {
        id: "draft",
        label: "The draft",
        run_id: "run-1",
        node_id: "draft",
        turn: null,
        value_digest: "a".repeat(64),
        media_type: "text/markdown",
      },
    ],
    ...overrides,
  };
}

describe("RunCard hierarchy", () => {
  it("puts the title, the owner decision, the result, and the one act on the card before the execution facts and the graph", () => {
    const { container } = render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({
              run_title: "Which cache do we use?",
              gates_orchestrated: ["quality-gate"],
              result: checked(),
            }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    const order = Array.from(container.querySelectorAll("[data-section]")).map((section) =>
      section.getAttribute("data-section"),
    );
    expect(order).toEqual([
      "title",
      "decision",
      "result",
      "artifacts",
      "action",
      "execution",
      "graph",
    ]);
  });

  it("folds the graph away until it is asked for", async () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ result: checked() }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    const toggle = screen.getByRole("button", { name: "Graph" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("draft")).not.toBeInTheDocument();

    await userEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("names the run by its own title and keeps the checked result beside it", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({
              run_title: "Which cache do we use?",
              result: checked({ status: "partial", attempted: 3, succeeded: 2, skipped: 1 }),
            }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByRole("link", { name: "Which cache do we use?" })).toBeInTheDocument();
    expect(screen.getByText("Partly done")).toBeInTheDocument();
    expect(screen.getByText("2 of 3 succeeded, 1 skipped, 0 failed.")).toBeInTheDocument();
  });

  it("names each artifact the run fixed, without fetching any of them", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ result: checked() }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByText("The draft")).toBeInTheDocument();
  });

  it("links each artifact it names to its own place on the run's page", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ result: checked() }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    // Named here, read there: a board full of cards never resolves an artifact, so the label is a
    // way to the page that does rather than a value this row fetched.
    const link = screen.getByRole("link", { name: "The draft" });
    expect(link).toHaveAttribute("href", "/workflows/wf.lit-check.v5?run=run-1#artifact-draft");
  });

  it("shows no recorded output on a board row, because a listing carries none", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({
              result: checked({ status: "noop", attempted: 0, succeeded: 0, primary_input: 0 }),
            }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    // The counters a workflow's author asked to show are the run page's, read off values a
    // listing row leaves out. The board shows the checked status and nothing derived from a
    // number the run wrote about itself.
    expect(screen.queryByText("Recorded output")).not.toBeInTheDocument();
    expect(screen.getByText("No work needed")).toBeInTheDocument();
  });

  it("says the result is unchecked for a record that carries none", () => {
    render(
      <ul>
        <RunCard
          card={{
            kind: "recent",
            run: runEnvelopeFixture({ result: null }),
            name: "Which cache do we use?",
            graph: GRAPH,
            onTogglePin: vi.fn(),
            pinning: false,
          }}
        />
      </ul>,
    );

    expect(screen.getByText("Unchecked")).toBeInTheDocument();
  });
});
