import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GraphNode, SpendView, StepEvent } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { RunTimeline, buildLanes, buildSpans, buildTicks, pickTickStep } from "./run-timeline";

function stepEvent(overrides: Partial<StepEvent> = {}): StepEvent {
  return { seq: 1, node: "draft", at: 0, finished: false, failed: false, ...overrides };
}

function graphNode(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    key: "draft",
    id: "draft",
    kind: "run",
    signature: "() -> str",
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
    ...overrides,
  };
}

// Every string this component renders -- visible text and title attributes alike -- may use
// only the punctuation the house style allows.
const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;

// A run's own spend record, its per-step readings keyed the way the ledger keys them: by the
// STEP name each leaf ran under, never by the node id the graph gave it.
function spendOf(steps: Record<string, string>): SpendView {
  const usage = { input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0 };
  return {
    cost_usd: "0.09",
    unpriced: 0,
    usage: { input_tokens: 100, output_tokens: 50, cache_read_tokens: 0, cache_write_tokens: 0 },
    steps: Object.fromEntries(
      Object.entries(steps).map(([name, cost_usd]) => [name, { cost_usd, unpriced: 0, usage }]),
    ),
  };
}

describe("buildSpans", () => {
  it("pairs a start with the next finish for the same node into one span", () => {
    const spans = buildSpans([
      stepEvent({ seq: 1, node: "draft", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "draft", at: 1.8, finished: true }),
    ]);

    expect(spans.get("draft")).toEqual([
      { startAt: 0, endAt: 1.8, failed: false, hasLaterSpan: false },
    ]);
  });

  it("gives a node two spans when a gate rejection reruns it", () => {
    const spans = buildSpans([
      stepEvent({ seq: 1, node: "gate", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "gate", at: 2, finished: true }),
      stepEvent({ seq: 3, node: "gate", at: 5, finished: false }),
      stepEvent({ seq: 4, node: "gate", at: 6, finished: true }),
    ]);

    const gateSpans = spans.get("gate");
    expect(gateSpans).toHaveLength(2);
    expect(gateSpans?.[0]).toEqual({ startAt: 0, endAt: 2, failed: false, hasLaterSpan: true });
    expect(gateSpans?.[1]).toEqual({ startAt: 5, endAt: 6, failed: false, hasLaterSpan: false });
  });

  it("leaves a span open when its node has started but not yet finished", () => {
    const spans = buildSpans([stepEvent({ seq: 1, node: "draft", at: 3, finished: false })]);

    expect(spans.get("draft")).toEqual([
      { startAt: 3, endAt: null, failed: false, hasLaterSpan: false },
    ]);
  });

  it("marks a span failed from its closing event", () => {
    const spans = buildSpans([
      stepEvent({ seq: 1, node: "draft", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "draft", at: 1.2, finished: true, failed: true }),
    ]);

    expect(spans.get("draft")?.[0]).toEqual({
      startAt: 0,
      endAt: 1.2,
      failed: true,
      hasLaterSpan: false,
    });
  });
});

describe("buildLanes", () => {
  it("orders ran lanes by first appearance, then appends never-ran nodes, params excluded", () => {
    const events = [
      stepEvent({ seq: 1, node: "gate", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "gate", at: 1, finished: true }),
      stepEvent({ seq: 3, node: "draft", at: 1, finished: false }),
      stepEvent({ seq: 4, node: "draft", at: 2, finished: true }),
    ];
    const nodes = [
      graphNode({ key: "prompt", id: "prompt", kind: "param" }),
      graphNode({ key: "draft", id: "draft", kind: "run" }),
      graphNode({ key: "gate", id: "gate", kind: "gate" }),
      graphNode({ key: "publish", id: "publish", kind: "run" }),
    ];

    const lanes = buildLanes(events, nodes);

    expect(lanes.map((lane) => lane.node)).toEqual(["gate", "draft", "publish"]);
    expect(lanes[2]).toMatchObject({ node: "publish", queued: true, spans: [] });
    expect(lanes.some((lane) => lane.node === "prompt")).toBe(false);
  });

  it("has no turnWorks on a lane whose node does not chain", () => {
    const events = [
      stepEvent({ seq: 1, node: "draft", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "draft", at: 1, finished: true }),
    ];
    const nodes = [graphNode({ key: "draft", id: "draft", kind: "run" })];

    expect(buildLanes(events, nodes)[0]).toMatchObject({ turnWorks: [] });
  });

  it("keeps today's stray-lane fallback for a __then id whose base names no chained map", () => {
    const events = [
      stepEvent({ seq: 1, node: "orphan__then", at: 0, finished: false }),
      stepEvent({ seq: 2, node: "orphan__then", at: 2, finished: true }),
    ];
    // "orphan" is not in the graph at all, and "digests" here chains nothing.
    const plainDigests = graphNode({ key: "digests", id: "digests", kind: "map" });

    const lanes = buildLanes(events, [plainDigests]);

    const stray = lanes.find((lane) => lane.node === "orphan__then");
    expect(stray).toMatchObject({ kind: "run", queued: false, turnWorks: [] });
    expect(stray?.spans).toHaveLength(1);
  });

  describe("a multi-turn map's later turns", () => {
    // "sources" here, not "digests" -- the same three-turn map the harness side's own graph
    // payload fixture uses, so a turn-1 dotted event id (`sources.digest_source`) is exactly
    // what a real payload sends, not a stand-in.
    const THREE_TURNS = graphNode({
      key: "sources",
      id: "sources",
      kind: "map",
      work: "digest_source",
      turns: [
        {
          work: "digest_source",
          node_id: "sources.digest_source",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "relate",
          node_id: "sources.relate",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "extract_references",
          node_id: "sources.extract_references",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
      ],
    });

    it("folds {id}.{work} step events into the map node's own lane, turn 1's own dotted id included", () => {
      const events = [
        stepEvent({ seq: 1, node: "sources.digest_source", at: 0, finished: false }),
        stepEvent({ seq: 2, node: "sources.digest_source", at: 2, finished: true }),
        stepEvent({ seq: 3, node: "sources.relate", at: 2, finished: false }),
        stepEvent({ seq: 4, node: "sources.relate", at: 5, finished: true }),
        stepEvent({ seq: 5, node: "sources.extract_references", at: 5, finished: false }),
        stepEvent({ seq: 6, node: "sources.extract_references", at: 8, finished: true }),
      ];

      const lanes = buildLanes(events, [THREE_TURNS]);

      expect(lanes).toHaveLength(1);
      expect(lanes[0]?.node).toBe("sources");
      expect(lanes[0]?.spans).toHaveLength(3);
      expect(lanes[0]?.turnWorks).toEqual(["digest_source", "relate", "extract_references"]);
    });

    it("still folds the archived __then convention onto a turns-carrying map, for a run recorded before turns shipped", () => {
      const events = [
        // A historical run's own recorded events, frozen at the convention the harness followed
        // when it actually ran -- the base per-item work under the map's own plain id, its one
        // chained leg under `__then`. The freshly rendered graph now describes the same chain as
        // three all-dotted `turns`, ids this historical run's own events never used.
        stepEvent({ seq: 1, node: "sources", at: 0, finished: false }),
        stepEvent({ seq: 2, node: "sources", at: 2, finished: true }),
        stepEvent({ seq: 3, node: "sources__then", at: 2, finished: false }),
        stepEvent({ seq: 4, node: "sources__then", at: 5, finished: true }),
      ];

      const lanes = buildLanes(events, [THREE_TURNS]);

      expect(lanes).toHaveLength(1);
      expect(lanes[0]?.node).toBe("sources");
      expect(lanes[0]?.spans).toHaveLength(2);
      expect(lanes[0]?.turnWorks).toEqual(["digest_source", "relate", "extract_references"]);
    });

    it("shows the map's own lane with turnWorks set even before any turn event arrives", () => {
      const lanes = buildLanes([], [THREE_TURNS]);

      expect(lanes).toEqual([
        {
          node: "sources",
          work: "digest_source",
          kind: "map",
          spans: [],
          queued: true,
          turnWorks: ["digest_source", "relate", "extract_references"],
        },
      ]);
    });

    it("keeps a single-turn map's lane plain, no turnWorks, when its turns entry is alone", () => {
      const oneTurn = graphNode({
        key: "digests",
        id: "digests",
        kind: "map",
        turns: [
          {
            work: "digest_source",
            node_id: "digests",
            briefing: "",
            tools: [],
            output_schema: null,
            source: "",
          },
        ],
      });

      expect(buildLanes([], [oneTurn])[0]).toMatchObject({ turnWorks: [] });
    });
  });
});

describe("pickTickStep and buildTicks", () => {
  it("picks a step keeping the axis within the tick budget for a mid-length run", () => {
    expect(pickTickStep(23)).toBe(5);
    expect(buildTicks(23)).toEqual([0, 5, 10, 15, 20]);
  });

  it("picks a coarser step for a longer run", () => {
    expect(pickTickStep(95)).toBe(30);
    expect(buildTicks(95)).toEqual([0, 30, 60, 90]);
  });

  it("falls back to a single tick when there is no span yet", () => {
    expect(pickTickStep(0)).toBe(1);
    expect(buildTicks(0)).toEqual([0]);
  });
});

// fetch runs once, draft runs, reruns after review rejects it, review is rejected once then
// approved, check is an orchestrator gate cleared once, publish is the still-open gate, notify
// never runs at all. `fetch` runs a step whose name is not its own node id, the way a spec that
// runs one step under a differently named node does, so a lane keyed by the wrong field shows it.
const NODES: GraphNode[] = [
  graphNode({ key: "fetch", id: "fetch", kind: "run", work: "find_undigested" }),
  graphNode({ key: "draft", id: "draft", kind: "run" }),
  graphNode({ key: "review", id: "review", kind: "gate" }),
  graphNode({ key: "check", id: "check", kind: "orchestrator-gate" }),
  graphNode({ key: "publish", id: "publish", kind: "gate" }),
  graphNode({ key: "notify", id: "notify", kind: "run" }),
];

const EVENTS: StepEvent[] = [
  stepEvent({ seq: 1, node: "fetch", at: 0, finished: false }),
  stepEvent({ seq: 2, node: "fetch", at: 40, finished: true }),
  stepEvent({ seq: 3, node: "draft", at: 40, finished: false }),
  stepEvent({ seq: 4, node: "draft", at: 130, finished: true }),
  stepEvent({ seq: 5, node: "review", at: 130, finished: false }),
  stepEvent({ seq: 6, node: "review", at: 210, finished: true }),
  stepEvent({ seq: 7, node: "draft", at: 210, finished: false }),
  stepEvent({ seq: 8, node: "draft", at: 260, finished: true }),
  stepEvent({ seq: 9, node: "review", at: 260, finished: false }),
  stepEvent({ seq: 10, node: "review", at: 290, finished: true }),
  stepEvent({ seq: 11, node: "check", at: 290, finished: false }),
  stepEvent({ seq: 12, node: "check", at: 305, finished: true }),
  stepEvent({ seq: 13, node: "publish", at: 305, finished: false }),
];

const STARTED_AT = "2026-08-20T10:00:00Z";

// 320s after the run's own start -- 15s past publish's own start at 305s.
const NOW = new Date("2026-08-20T10:05:20Z");

describe("RunTimeline", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders nothing when there are no events", () => {
    render(<RunTimeline events={[]} nodes={[]} onSelectNode={vi.fn()} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("speaks the legend in the design's own words, Failed absent when nothing failed", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    for (const word of [
      "Ran",
      "Running",
      "Cleared by a person",
      "Cleared by the agent",
      "Waiting",
    ]) {
      expect(screen.getByText(word)).toBeInTheDocument();
    }
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("adds Failed to the legend only once this run actually failed something", () => {
    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "draft", at: 0, finished: false }),
          stepEvent({ seq: 2, node: "draft", at: 1.2, finished: true, failed: true }),
        ]}
        nodes={[graphNode({ key: "draft", id: "draft", kind: "run" })]}
        onSelectNode={vi.fn()}
      />,
    );

    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("draws one bar per event, so a rerun reads as two bars in the same lane", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    const draftBars = screen.getAllByTitle(/^draft,/);
    expect(draftBars).toHaveLength(2);
    expect(screen.getByTitle("draft, 90.0s.")).toBeInTheDocument();
    expect(screen.getByTitle("draft, 50.0s.")).toBeInTheDocument();
  });

  it("carries no visible caption inside a bar, the state read from its title instead", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    const bar = screen.getByTitle("draft, 90.0s.");
    expect(bar.textContent).toBe("");
  });

  it("marks a rejected gate visit with a vermilion person marker, its cleared visit with an ink one", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    const rejected = screen.getByTitle("review, rejected, 80.0s.");
    const rejectedMarker = rejected.querySelector("[data-glyph-fill]");
    expect(rejectedMarker).toHaveAttribute("data-glyph-kind", "person-gate");
    expect(rejectedMarker).toHaveAttribute("data-glyph-fill", "failed");

    const cleared = screen.getByTitle("review, cleared, 30.0s.");
    const clearedMarker = cleared.querySelector("[data-glyph-fill]");
    expect(clearedMarker).toHaveAttribute("data-glyph-kind", "person-gate");
    expect(clearedMarker).toHaveAttribute("data-glyph-fill", "ran");
  });

  it("marks an orchestrator gate's own cleared visit with the agent's hexagon", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    const cleared = screen.getByTitle("check, cleared, 15.0s.");
    const marker = cleared.querySelector("[data-glyph-fill]");
    expect(marker).toHaveAttribute("data-glyph-kind", "agent-gate");
    expect(marker).toHaveAttribute("data-glyph-fill", "ran");
  });

  it("marks a failed step's bar with the word failed, not color alone", () => {
    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "draft", at: 0, finished: false }),
          stepEvent({ seq: 2, node: "draft", at: 1.2, finished: true, failed: true }),
        ]}
        nodes={[graphNode({ key: "draft", id: "draft", kind: "run" })]}
        onSelectNode={vi.fn()}
      />,
    );

    expect(screen.getByTitle("draft, 1.2s failed.")).toBeInTheDocument();
  });

  it("runs the open gate's own segment to the now edge, with no marker at its end", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        live
        startedAt={STARTED_AT}
      />,
    );

    const open = screen.getByTitle("publish, waiting, 15.0s.");
    expect(open.querySelector("[data-glyph-fill]")).toBeNull();
    expect(open.style.left).toBe("95.3125%");
    expect(screen.getByText("now")).toBeInTheDocument();
  });

  it("shows no now cursor once the run is no longer live", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    expect(screen.queryByText("now")).not.toBeInTheDocument();
  });

  it("draws the not-reached rectangle from the run's current position to the right edge", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const onSelectNode = vi.fn();
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={onSelectNode}
        live
        startedAt={STARTED_AT}
      />,
    );

    const queued = screen.getByTitle("notify has not run yet.");
    expect(queued.style.left).toBe("95.3125%");
    expect(queued).toHaveAttribute("aria-label", "notify has not run yet.");

    // The position is read off already; the click itself needs no fake clock, and
    // user-event's own internal waits do not get on with a faked one.
    vi.useRealTimers();
    await userEvent.click(queued);
    expect(onSelectNode).toHaveBeenCalledWith("notify");
  });

  it("names the clicked bar's node to the caller", async () => {
    const onSelectNode = vi.fn();
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={onSelectNode} />);

    await userEvent.click(screen.getByTitle("fetch, 40.0s."));

    expect(onSelectNode).toHaveBeenCalledTimes(1);
    expect(onSelectNode).toHaveBeenCalledWith("fetch");
  });

  it("reads a finished lane's own right gutter as the elapsed run time at which it closed", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    expect(screen.getByText("40s")).toBeInTheDocument();
    expect(screen.getByText("4m 20s")).toBeInTheDocument();
    expect(screen.getByText("4m 50s")).toBeInTheDocument();
    expect(screen.getByText("5m 5s")).toBeInTheDocument();
  });

  it("shows a lane's own step cost under its gutter time, keyed by the step name its node runs", () => {
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        spend={spendOf({ find_undigested: "0.0042" })}
      />,
    );

    expect(screen.getByText("$0.0042")).toBeInTheDocument();
  });

  it("shows no cost for a lane whose node id, rather than its step name, is the record's key", () => {
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        spend={spendOf({ fetch: "0.0042" })}
      />,
    );

    expect(screen.queryByText("$0.0042")).not.toBeInTheDocument();
  });

  it("falls back to a lane's node id when its node names no step of its own", () => {
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        spend={spendOf({ draft: "0.0031" })}
      />,
    );

    expect(screen.getByText("$0.0031")).toBeInTheDocument();
  });

  it("adds a later turn's own cost to the map lane it folds into, turn 1's own dotted id included", () => {
    const digests = graphNode({
      key: "digests",
      id: "digests",
      kind: "map",
      work: "digest_source",
      turns: [
        {
          work: "digest_source",
          node_id: "digests.digest_source",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "relate",
          node_id: "digests.relate",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
      ],
    });

    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "digests.digest_source", at: 0, finished: false }),
          stepEvent({ seq: 2, node: "digests.digest_source", at: 5, finished: true }),
          stepEvent({ seq: 3, node: "digests.relate", at: 5, finished: false }),
          stepEvent({ seq: 4, node: "digests.relate", at: 8, finished: true }),
        ]}
        nodes={[digests]}
        onSelectNode={vi.fn()}
        spend={spendOf({ digest_source: "0.003", relate: "0.0012" })}
      />,
    );

    expect(screen.getByText("$0.0042")).toBeInTheDocument();
  });

  it("shows no cost on either lane when two nodes run the same step, because neither can claim the bucket", () => {
    const first = graphNode({ key: "first", id: "first", kind: "run", work: "summarise" });
    const second = graphNode({ key: "second", id: "second", kind: "run", work: "summarise" });

    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "first", at: 0, finished: true }),
          stepEvent({ seq: 2, node: "second", at: 5, finished: true }),
        ]}
        nodes={[first, second]}
        onSelectNode={vi.fn()}
        spend={spendOf({ summarise: "0.0042" })}
      />,
    );

    // The run's spend is keyed by step, so one bucket holds both nodes' work. Printing it on each
    // lane would read as two costs and add up past what the run actually spent.
    expect(screen.queryByText("$0.0042")).not.toBeInTheDocument();
  });

  it("counts a chained map's bucket once when its own work and its chained work are the same step", () => {
    // Both turns are named "summarise", so both dot to the same id -- "looping.summarise" is
    // where every one of this map's own calls reports, turn 1 included.
    const looping = graphNode({
      key: "looping",
      id: "looping",
      kind: "map",
      work: "summarise",
      turns: [
        {
          work: "summarise",
          node_id: "looping.summarise",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "summarise",
          node_id: "looping.summarise",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
      ],
    });

    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "looping.summarise", at: 0, finished: true }),
          stepEvent({ seq: 2, node: "looping.summarise", at: 5, finished: true }),
        ]}
        nodes={[looping]}
        onSelectNode={vi.fn()}
        spend={spendOf({ summarise: "0.0042" })}
      />,
    );

    // The assertion that matters is the POSITIVE one. Asserting only that the doubled figure is
    // absent would also pass with no figure rendered at all, which is a different bug.
    expect(screen.getByText("$0.0042")).toBeInTheDocument();
    expect(screen.queryByText("$0.0084")).not.toBeInTheDocument();
  });

  it("shows no cost under any lane's gutter time when the run carries no spend record", () => {
    render(<RunTimeline events={EVENTS} nodes={NODES} onSelectNode={vi.fn()} />);

    expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
  });

  it("reads the open gate's own right gutter as its live wait, in warn ink, empty for not reached", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        live
        startedAt={STARTED_AT}
      />,
    );

    const wait = screen.getByText("15s");
    expect(wait).toHaveClass("text-warn-ink");
    const notifyRow = screen.getByTitle("notify has not run yet.").closest('[class*="grid-cols-"]');
    const gutter = notifyRow?.querySelector(".text-right");
    expect(gutter).toHaveTextContent("");
  });

  it("uses only the house's allowed punctuation in every rendered string", () => {
    const { container } = render(
      <RunTimeline
        events={EVENTS}
        nodes={NODES}
        onSelectNode={vi.fn()}
        live
        startedAt={STARTED_AT}
      />,
    );

    expect(container.textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
    for (const element of container.querySelectorAll("[title]")) {
      expect(element.getAttribute("title") ?? "").toMatch(ALLOWED_PUNCTUATION);
    }
    for (const element of container.querySelectorAll("[aria-label]")) {
      expect(element.getAttribute("aria-label") ?? "").toMatch(ALLOWED_PUNCTUATION);
    }
  });
});

describe("RunTimeline, the archived __then convention", () => {
  it("keeps today's stray-lane fallback for a __then id whose base does not chain", () => {
    const onSelectNode = vi.fn();
    const plainNodes = [graphNode({ key: "digests", id: "digests", kind: "map" })];
    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "orphan__then", at: 0, finished: false }),
          stepEvent({ seq: 2, node: "orphan__then", at: 2, finished: true }),
        ]}
        nodes={plainNodes}
        onSelectNode={onSelectNode}
      />,
    );

    expect(screen.getByText("orphan__then")).toBeInTheDocument();
  });
});

describe("RunTimeline, a multi-turn map's own lane", () => {
  const TURN_NODES: GraphNode[] = [
    graphNode({
      key: "digests",
      id: "digests",
      kind: "map",
      work: "digest_source",
      turns: [
        {
          work: "digest_source",
          node_id: "digests.digest_source",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "relate",
          node_id: "digests.relate",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
        {
          work: "extract_references",
          node_id: "digests.extract_references",
          briefing: "",
          tools: [],
          output_schema: null,
          source: "",
        },
      ],
    }),
  ];

  // digests runs its first turn, then its second and third -- each reported under its own
  // `{id}.{work}` id per the turn's own node_id. Every turn dots, first included -- only a
  // single-turn map's one turn shares the map's own plain id.
  const TURN_EVENTS: StepEvent[] = [
    stepEvent({ seq: 1, node: "digests.digest_source", at: 0, finished: false }),
    stepEvent({ seq: 2, node: "digests.digest_source", at: 2, finished: true }),
    stepEvent({ seq: 3, node: "digests.relate", at: 2, finished: false }),
    stepEvent({ seq: 4, node: "digests.relate", at: 5, finished: true }),
    stepEvent({ seq: 5, node: "digests.extract_references", at: 5, finished: false }),
    stepEvent({ seq: 6, node: "digests.extract_references", at: 9, finished: true }),
  ];

  it("draws one lane for the map node, all three turns' bars on it, turn 1's own dotted id included", () => {
    render(<RunTimeline events={TURN_EVENTS} nodes={TURN_NODES} onSelectNode={vi.fn()} />);

    expect(screen.getAllByText("digests")).toHaveLength(1);
    expect(screen.queryByText("digests.digest_source")).not.toBeInTheDocument();
    expect(screen.queryByText("digests.relate")).not.toBeInTheDocument();
    expect(screen.queryByText("digests.extract_references")).not.toBeInTheDocument();
    expect(screen.getByTitle("digests, 2.0s.")).toBeInTheDocument();
    expect(screen.getByTitle("digests, 3.0s.")).toBeInTheDocument();
  });

  it("shows the turn works joined with then as a secondary line under the map node's own id", () => {
    render(<RunTimeline events={TURN_EVENTS} nodes={TURN_NODES} onSelectNode={vi.fn()} />);

    expect(
      screen.getByText("digest_source then relate then extract_references"),
    ).toBeInTheDocument();
  });

  it("routes a click on a later turn's own bar to the map node, not the derived id", async () => {
    const onSelectNode = vi.fn();
    render(<RunTimeline events={TURN_EVENTS} nodes={TURN_NODES} onSelectNode={onSelectNode} />);

    await userEvent.click(screen.getByTitle("digests, 3.0s."));

    expect(onSelectNode).toHaveBeenCalledWith("digests");
    expect(onSelectNode).not.toHaveBeenCalledWith("digests.relate");
  });

  it("adds every turn's own priced cost to the one map lane, summed once each", () => {
    render(
      <RunTimeline
        events={TURN_EVENTS}
        nodes={TURN_NODES}
        onSelectNode={vi.fn()}
        spend={spendOf({ digest_source: "0.003", relate: "0.0012", extract_references: "0.0008" })}
      />,
    );

    expect(screen.getByText("$0.0050")).toBeInTheDocument();
  });

  it("suppresses a multi-turn map's figure when a later turn's own step name collides with another lane", () => {
    const other = graphNode({ key: "relate", id: "relate", kind: "run", work: "relate" });

    render(
      <RunTimeline
        events={[...TURN_EVENTS, stepEvent({ seq: 7, node: "relate", at: 8, finished: true })]}
        nodes={[...TURN_NODES, other]}
        onSelectNode={vi.fn()}
        spend={spendOf({ digest_source: "0.003", relate: "0.0012", extract_references: "0.0008" })}
      />,
    );

    // "relate" is read by both the map's own lane (its second turn) and the plain "relate" lane,
    // so the run's one bucket for it cannot be shown on either without doubling it.
    expect(screen.queryByText("$0.0050")).not.toBeInTheDocument();
    expect(screen.queryByText("$0.0012")).not.toBeInTheDocument();
  });

  // The exact same property, pinned against a plain then-map, lives in the "RunTimeline" describe
  // above as "counts a chained map's bucket once when its own work and its chained work are the
  // same step" -- no need to repeat it here.

  it("keeps a single-turn map's lane unchanged, no secondary line, when its turns entry is alone", () => {
    const oneTurn = [
      graphNode({
        key: "digests",
        id: "digests",
        kind: "map",
        turns: [
          {
            work: "digest_source",
            node_id: "digests",
            briefing: "",
            tools: [],
            output_schema: null,
            source: "",
          },
        ],
      }),
    ];
    render(
      <RunTimeline
        events={[
          stepEvent({ seq: 1, node: "digests", at: 0, finished: false }),
          stepEvent({ seq: 2, node: "digests", at: 5, finished: true }),
        ]}
        nodes={oneTurn}
        onSelectNode={vi.fn()}
      />,
    );

    expect(screen.getByText("digests")).toBeInTheDocument();
    expect(screen.queryByText(/then/)).not.toBeInTheDocument();
  });

  it("uses only the house's allowed punctuation on the multi-turn lane's own label", () => {
    const { container } = render(
      <RunTimeline events={TURN_EVENTS} nodes={TURN_NODES} onSelectNode={vi.fn()} />,
    );

    expect(container.textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
  });
});
