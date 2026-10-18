import { fireEvent, screen, waitFor } from "@testing-library/dom";
import { describe, expect, it, vi } from "vitest";

import type { WorkflowGraph } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { WorkflowCanvas } from "./workflow-canvas";

// jsdom has no real layout (every offsetWidth is 0) and no canvas 2d context,
// so these tests pin the component's STRUCTURE, a card per node, a path per
// edge, run state written onto the card, and leave geometry to the pure
// studio-layout/studio-camera suites, which is where it is actually computed.

const GRAPH: WorkflowGraph = {
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
      output_schema: null,
      source: "",
    },
    {
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "compose_decision_record(context: string) → DecisionRecord",
      detail: "Write the record.",
      tools: ["run_sql"],
      required: true,
      is_output: false,
      unknown: false,
      brought: true,
      briefing: "Compose a dated ADR.",
      input_schemas: {},
      output_schema: null,
      source: "",
    },
    {
      key: "gate",
      id: "gate",
      kind: "gate",
      signature: "a human approves, DecisionRecord passes through",
      detail: "rejected → draft, which reruns from there",
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
    {
      key: "record",
      id: "record",
      kind: "run",
      signature: "store_decision(record: DecisionRecord) → None",
      detail: "File the approved record.",
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
  edges: [
    { from: "param:context", to: "draft", kind: "wire", label: "context: string" },
    { from: "draft", to: "gate", kind: "wire", label: "reviews: DecisionRecord" },
    { from: "gate", to: "draft", kind: "rejected", label: "rejected" },
    { from: "gate", to: "record", kind: "wire", label: "record: DecisionRecord" },
  ],
};

// A graph carrying the new orchestrator-gate node kind -- the regression that matters: this
// kind used to fail the schema boundary entirely (studio-types.ts rejected it), which broke the
// whole page's parse the moment one workflow's graph carried one. Kept separate from `GRAPH`
// so the shared fixture's own node/edge/handle counts stay exact.
const ORCHESTRATOR_GRAPH: WorkflowGraph = {
  name: "lit-check",
  version: 6,
  nodes: [
    {
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "compose_decision_record(context: string) → DecisionRecord",
      detail: "Write the record.",
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
    {
      key: "review",
      id: "review",
      kind: "orchestrator-gate",
      signature: "this run's orchestrator decides, DecisionRecord passes through",
      detail: "The orchestrator decides, not a person, against the charge below.",
      tools: [],
      required: true,
      is_output: false,
      unknown: false,
      brought: false,
      briefing:
        "Check the record cites at least one prior decision and states its risk level before it clears.",
      input_schemas: {},
      output_schema: null,
      source: "",
    },
    {
      key: "record",
      id: "record",
      kind: "run",
      signature: "store_decision(record: DecisionRecord) → None",
      detail: "File the approved record.",
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
  edges: [
    { from: "draft", to: "review", kind: "wire", label: "reviews: DecisionRecord" },
    { from: "review", to: "draft", kind: "rejected", label: "rejected" },
    { from: "review", to: "record", kind: "wire", label: "record: DecisionRecord" },
  ],
};

// A map node that chains "relate" on each of its own results, and the same node with the chain
// stripped out -- the pair the "then" line test asserts on and off.
const CHAINED_MAP_GRAPH: WorkflowGraph = {
  name: "chained-map",
  version: 1,
  nodes: [
    {
      key: "items",
      id: "items",
      kind: "run",
      signature: "list_items() → list[Item]",
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
    {
      key: "digests",
      id: "digests",
      kind: "map",
      signature: "digest_source(source: Item) → DigestResult, then relate once per result",
      detail: "Digests each item, then runs 'relate' on each result.",
      tools: [],
      required: true,
      is_output: true,
      unknown: false,
      brought: true,
      briefing: "",
      input_schemas: {},
      output_schema: null,
      source: "",
      turns: [
        {
          work: "digest_source",
          node_id: "digests",
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
          output_schema: { type: "object", title: "RelateResult" },
          source: "",
        },
      ],
    },
  ],
  edges: [{ from: "items", to: "digests", kind: "map", label: "each source: Item" }],
};

const PLAIN_MAP_GRAPH: WorkflowGraph = {
  ...CHAINED_MAP_GRAPH,
  nodes: CHAINED_MAP_GRAPH.nodes.map((n) =>
    n.id === "digests"
      ? {
          ...n,
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
        }
      : n,
  ),
};

describe("WorkflowCanvas map chaining", () => {
  it("names the chained work on a chained map card's tooltip, and not on a plain map's", () => {
    const chained = render(<WorkflowCanvas graph={CHAINED_MAP_GRAPH} />);
    // The card's own chained-work line, distinct from any "then" the signature prose carries.
    expect(
      chained.container.querySelector('[data-node="digests"]')?.getAttribute("title"),
    ).toMatch(/\nthen relate/);
    chained.unmount();
    const plain = render(<WorkflowCanvas graph={PLAIN_MAP_GRAPH} />);
    expect(
      plain.container.querySelector('[data-node="digests"]')?.getAttribute("title"),
    ).not.toMatch(/\nthen /);
  });
});

describe("WorkflowCanvas", () => {
  it("draws a card per node and a wire per edge", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    // "draft" also appears as the value of the gate's reviews/rejects-to facts now,
    // so the node's own title is asserted scoped to its card, not document-wide.
    expect(container.querySelector('[data-node="draft"]')?.textContent).toContain("draft");
    expect(screen.getByText("context")).toBeInTheDocument();
    expect(container.querySelectorAll("[data-node]")).toHaveLength(4);
    expect(container.querySelectorAll("svg [data-edge]")).toHaveLength(4);
  });

  it("shows a param's type as a fact line", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const fact = container.querySelector('[data-node="param:context"] [data-fact="type"]');
    expect(fact?.textContent).toContain("string");
  });

  it("carries a run node's signature on its tooltip, off the card face", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const card = container.querySelector('[data-node="draft"]');
    expect(card?.getAttribute("title")).toContain("compose_decision_record");
    expect(card?.textContent).not.toContain("compose_decision_record(");
  });

  it("marks a gate with a non-rect shape and carries its routing on the tooltip", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const gate = container.querySelector('[data-node="gate"]');
    // A gate a person owns is a diamond, distinct from the agent gate's hexagon --
    // the two must never share one.
    expect(gate?.getAttribute("data-shape")).toBe("diamond");
    expect(gate?.getAttribute("data-shape")).not.toBe("rect");
    expect(gate?.getAttribute("title")).toContain("rejects to draft");
  });

  it("pips the approved wire at its source, with a glyph and the word, unaffected by run data", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const approved = container.querySelector('[data-pip="approved"]');
    expect(approved?.textContent).toContain("approved");
    expect(approved?.querySelector("svg")).not.toBeNull();
    // A plain data-carrying wire label (not a condition) stays the old floating badge.
    expect(container.querySelector('[data-badge="neutral"]')).not.toBeNull();
  });

  it("labels the rejection arc 'on rejection', a wiring fact, when no run has rejected anything", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const label = container.querySelector('[data-arc-label="on-rejection"]');
    expect(label?.textContent).toContain("on rejection");
    expect(label?.querySelector("svg")).toBeNull();
    // The amber, glyphed pip is an event -- it never appears with no run data.
    expect(container.querySelector('[data-pip="rejected"]')).toBeNull();
  });

  it("pips the rejection arc once the viewed run's gate actually rejected, with the count past one", () => {
    const { container, rerender } = render(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: [], done: [], failed: [], waitingAt: null, rejected: { gate: 1 } }}
      />,
    );
    const pip = container.querySelector('[data-pip="rejected"]');
    expect(pip?.textContent).toContain("rejected");
    expect(pip?.textContent).not.toContain("(1)");
    expect(pip?.querySelector("svg")).not.toBeNull();
    expect(container.querySelector('[data-arc-label="on-rejection"]')).toBeNull();

    rerender(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: [], done: [], failed: [], waitingAt: null, rejected: { gate: 2 } }}
      />,
    );
    expect(container.querySelector('[data-pip="rejected"]')?.textContent).toContain("rejected (2)");
  });

  it("paints the canvas surface with a dotted grid", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    expect(container.querySelector(".workflow-canvas-grid")).not.toBeNull();
  });

  it("marks a carried step with the package symbol, not a word", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const packed = container.querySelector('[data-node="draft"] [data-carried]');
    expect(packed).not.toBeNull();
    expect(packed?.getAttribute("title")).toContain("travels with the workflow");
    expect(container.querySelector('[data-node="gate"] [data-carried]')).toBeNull();
  });

  it("shows each step's product type as its one fact line", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const product = container.querySelector('[data-node="draft"] [data-fact="product"]');
    expect(product?.textContent).toBe("gives DecisionRecord");
    // A gate hands through what it reviewed; it makes nothing, so it claims no product.
    expect(container.querySelector('[data-node="gate"] [data-fact="product"]')).toBeNull();
  });

  it("wears the runner as the step icon: the bot only where an agent actually runs", () => {
    const briefed = GRAPH.nodes.find((n) => n.key === "draft");
    if (!briefed) throw new Error("fixture lost its draft node");
    const graph: WorkflowGraph = {
      ...GRAPH,
      nodes: [
        ...GRAPH.nodes,
        { ...briefed, key: "plain", id: "plain", tools: [], briefing: "" },
        { ...briefed, key: "program", id: "program", tools: [], briefing: "", source: "1 + 1" },
      ],
    };
    const { container } = render(<WorkflowCanvas graph={graph} />);
    const svgOf = (key: string) =>
      container.querySelector(`[data-node="${key}"] [data-kind-icon] svg`)?.getAttribute("class");
    expect(svgOf("draft")).toContain("bot");
    expect(svgOf("plain")).toContain("wrench");
    expect(svgOf("program")).toContain("braces");
  });

  it("leads each card's title row with an icon for its kind", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    expect(
      container.querySelector('[data-node="param:context"] [data-kind-icon="param"] svg'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-node="draft"] [data-kind-icon="run"] svg'),
    ).not.toBeNull();
    expect(
      container.querySelector('[data-node="gate"] [data-kind-icon="gate"] svg'),
    ).not.toBeNull();
  });

  it("gives a running node a motion-safe glow, a static ring otherwise", () => {
    const { container } = render(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: ["draft"], done: ["record"], failed: [], waitingAt: null }}
      />,
    );
    const running = container.querySelector('[data-node="draft"]');
    expect(running?.className).toMatch(/workflow-canvas-node--running/);
    expect(running?.className).toMatch(/motion-safe:animate-\[workflow-canvas-glow/);

    // "gate" never entered a run state here, so it stays idle: no glow class at all,
    // gating the animation behind motion-safe rather than always-on regardless of
    // the viewer's reduced-motion preference is the whole point of the class.
    const idle = container.querySelector('[data-node="gate"]');
    expect(idle?.className).not.toMatch(/workflow-canvas-node--running/);
    expect(idle?.className).not.toMatch(/motion-safe:animate-/);

    // "done" gets a quiet tick in the header row instead of a glow.
    const done = container.querySelector('[data-node="record"]');
    expect(done?.querySelector('[data-state-tick="done"] svg')).not.toBeNull();
  });

  it("writes the run's progress onto the cards it concerns", () => {
    const { container } = render(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: ["draft"], done: [], failed: [], waitingAt: "gate" }}
      />,
    );
    expect(container.querySelector('[data-node="draft"]')?.getAttribute("data-state")).toBe(
      "active",
    );
    expect(container.querySelector('[data-node="gate"]')?.getAttribute("data-state")).toBe(
      "waiting",
    );
    expect(
      container.querySelector('[data-node="param:context"]')?.getAttribute("data-state"),
    ).toBeNull();
  });

  it("hands a clicked card's node to onNodeClick", () => {
    const onNodeClick = vi.fn();
    const { container } = render(<WorkflowCanvas graph={GRAPH} onNodeClick={onNodeClick} />);
    // A bare click, not the full press sequence: React Flow's drag handling
    // lives on mousedown and needs real layout jsdom does not have.
    const card = container.querySelector('[data-node="draft"]');
    expect(card).not.toBeNull();
    if (card) fireEvent.click(card);
    expect(onNodeClick).toHaveBeenCalledWith(GRAPH.nodes[1]);
  });

  it("keeps every card to its header zone, the context on the tooltip rather than a body", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const step = container.querySelector('[data-node="draft"]');
    expect(step?.querySelector('[data-zone="header"]')?.textContent).toContain("draft");
    expect(step?.querySelector('[data-zone="body"]')).toBeNull();

    const gate = container.querySelector('[data-node="gate"]');
    expect(gate?.querySelector('[data-zone="header"]')).not.toBeNull();
    expect(gate?.querySelector('[data-zone="body"]')).toBeNull();
    expect(gate?.getAttribute("title")).toContain("reviews draft");
  });

  it("renders a param as a circle with its name and type as labels outside the shape", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const param = container.querySelector('[data-node="param:context"]');
    expect(param?.getAttribute("data-shape")).toBe("circle");
    const circle = param?.querySelector('[data-kind-icon="param"]');
    expect(circle).not.toBeNull();
    expect(circle?.querySelector("svg")).not.toBeNull();
    // the name and its type read as labels beside the circle, not inside it
    expect(circle?.textContent).not.toContain("context");
    expect(param?.textContent).toContain("context");
    expect(param?.querySelector('[data-fact="type"]')?.textContent).toContain("string");
  });

  it("keeps a handle anchor on every node for edges to attach to, though no connector dot is drawn", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const sources = container.querySelectorAll(".workflow-canvas-handle--source");
    const targets = container.querySelectorAll(".workflow-canvas-handle--target");
    // one of each per node: param, draft, gate, record
    expect(sources.length).toBe(4);
    expect(targets.length).toBe(4);
  });

  it("gives each kind its own node-glyph shape in the header, a person gate distinct from a step", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    expect(
      container
        .querySelector('[data-node="draft"] [data-glyph-kind]')
        ?.getAttribute("data-glyph-kind"),
    ).toBe("step");
    expect(
      container
        .querySelector('[data-node="gate"] [data-glyph-kind]')
        ?.getAttribute("data-glyph-kind"),
    ).toBe("person-gate");
  });

  it("draws the rejection edge as a side arc built from straight segments, never a smooth curve through the column", async () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    // The layout lands asynchronously; until it does the edge is a live bezier.
    await waitFor(() => {
      const path = container.querySelector('g[data-edge^="gate->draft"] path');
      expect(path).not.toBeNull();
      const d = path?.getAttribute("d") ?? "";
      // source -> bow -> bow -> target: three straight segments, never a bezier curve.
      expect((d.match(/L/g) ?? []).length).toBe(3);
      expect(d).not.toContain("C");
    });
  });

  it("fades a node downstream of the still-waiting gate, and the wire feeding it, until the run reaches it", () => {
    const { container } = render(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: [], done: [], failed: [], waitingAt: "gate" }}
      />,
    );
    const queuedNode = container.querySelector('[data-node="record"]');
    expect(queuedNode?.getAttribute("data-queued")).toBe("true");
    expect(queuedNode?.className).toMatch(/opacity-50/);
    const feedingWire = container.querySelector('g[data-edge^="gate->record"]');
    expect(feedingWire?.getAttribute("class")).toMatch(/opacity-50/);

    // The gate itself is waiting, not queued -- queued only ever describes what
    // sits downstream of an unresolved gate, never the gate itself.
    const gate = container.querySelector('[data-node="gate"]');
    expect(gate?.getAttribute("data-queued")).toBeNull();
  });

  it("stacks a condition pip's circular glyph above its word", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const approved = container.querySelector('[data-pip="approved"]');
    expect(approved).not.toBeNull();
    const children = Array.from(approved?.children ?? []);
    expect(children).toHaveLength(2);
    const [glyph, word] = children;
    expect(glyph?.querySelector("svg")).not.toBeNull();
    expect(glyph?.textContent).toBe("");
    expect(word?.querySelector("svg")).toBeNull();
    expect(word?.textContent).toContain("approved");
  });

  it("marks a clicked card selected with the dashed-outline class and a motion-safe lift", () => {
    const { container } = render(<WorkflowCanvas graph={GRAPH} />);
    const card = container.querySelector('[data-node="draft"]');
    expect(card).not.toBeNull();
    if (card) fireEvent.click(card);
    expect(card?.className).toMatch(/workflow-canvas-node--selected/);
    expect(card?.className).toMatch(/motion-safe:scale-\[1\.02\]/);
    // the ring-based selection styling is gone; selection is the outline class alone
    expect(card?.className).not.toMatch(/workflow-canvas-node--ring/);
    // the selection outline keys off the node's own state color;
    // "draft" carries no notable state here, so it selects with the neutral ink tone.
    expect(card?.getAttribute("data-selected")).toBe("true");
    expect(card?.getAttribute("data-select-tone")).toBe("ink");
  });

  it("selects a waiting gate with the vermilion tone, not the neutral one", () => {
    const { container } = render(
      <WorkflowCanvas
        graph={GRAPH}
        runState={{ active: [], done: [], failed: [], waitingAt: "gate" }}
      />,
    );
    const gate = container.querySelector('[data-node="gate"]');
    expect(gate).not.toBeNull();
    if (gate) fireEvent.click(gate);
    expect(gate?.getAttribute("data-select-tone")).toBe("warn");
  });

  it("offers fit and zoom controls, and an empty graph explains itself", () => {
    render(<WorkflowCanvas graph={GRAPH} />);
    expect(screen.getByRole("button", { name: /fit view/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /zoom in/i })).toBeInTheDocument();

    render(<WorkflowCanvas graph={{ name: "", version: 0, nodes: [], edges: [] }} />);
    expect(screen.getByText(/nothing to draw yet/i)).toBeInTheDocument();
  });
});

describe("WorkflowCanvas orchestrator gate node", () => {
  it("parses and renders the card -- the graph that used to break the whole page", () => {
    const { container } = render(<WorkflowCanvas graph={ORCHESTRATOR_GRAPH} />);
    expect(container.querySelectorAll("[data-node]")).toHaveLength(3);
    const card = container.querySelector('[data-node="review"]');
    expect(card).not.toBeNull();
    expect(card?.getAttribute("data-shape")).toBe("hex");
  });

  it("gives the card its own icon and kind chip, distinct from the person gate's", () => {
    const { container } = render(<WorkflowCanvas graph={ORCHESTRATOR_GRAPH} />);
    const chip = container.querySelector(
      '[data-node="review"] [data-kind-icon="orchestrator-gate"]',
    );
    const svg = chip?.querySelector("svg");
    expect(svg).not.toBeNull();
    // the person gate's warn tone must not leak onto the orchestrator gate's own icon or chip
    expect(svg?.getAttribute("class")).not.toMatch(/text-warn-ink/);
    expect(svg?.getAttribute("class")).toMatch(/text-accent\b/);
    expect(chip?.className).not.toMatch(/bg-warn-soft/);
    expect(screen.getByText("orchestrator gate")).toBeInTheDocument();
  });

  it("keeps the charge and routing on the tooltip, the state on the card's own marker", () => {
    const { container } = render(
      <WorkflowCanvas
        graph={ORCHESTRATOR_GRAPH}
        runState={{ active: [], done: ["draft"], failed: [], waitingAt: "review" }}
      />,
    );
    const card = container.querySelector('[data-node="review"]');
    expect(card?.getAttribute("title")).toContain(
      "Check the record cites at least one prior decision and states its risk level before it clears.",
    );
    expect(card?.getAttribute("title")).toContain("rejects to draft");
    expect(card?.getAttribute("data-state")).toBe("waiting");
    expect(card?.textContent).not.toContain("Check the record cites");
  });

  it("treats the rejection back-edge and the approved pip exactly like the person gate", () => {
    const { container } = render(<WorkflowCanvas graph={ORCHESTRATOR_GRAPH} />);
    expect(container.querySelectorAll("svg [data-edge]")).toHaveLength(3);
    const approved = container.querySelector('[data-pip="approved"]');
    expect(approved?.textContent).toContain("approved");
    const onRejection = container.querySelector('[data-arc-label="on-rejection"]');
    expect(onRejection?.textContent).toContain("on rejection");
  });

  it("uses only the house's allowed punctuation on the card", () => {
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;
    const { container } = render(<WorkflowCanvas graph={ORCHESTRATOR_GRAPH} />);
    const card = container.querySelector('[data-node="review"]');
    expect(card?.textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
  });
});

describe("WorkflowCanvas orientation", () => {
  it("offers Auto, Down, and Across, with Auto pressed by default and naming its pick", () => {
    render(<WorkflowCanvas graph={GRAPH} />);

    const group = screen.getByRole("group", { name: "Orientation" });
    expect(group).toHaveTextContent("Auto (down)");
    expect(screen.getByRole("button", { name: /^Auto/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Down" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Across" })).toHaveAttribute("aria-pressed", "false");
  });

  it("presses the orientation a reader picks by hand", () => {
    render(<WorkflowCanvas graph={GRAPH} />);

    fireEvent.click(screen.getByRole("button", { name: "Across" }));
    expect(screen.getByRole("button", { name: "Across" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^Auto/ })).toHaveAttribute("aria-pressed", "false");
  });
});
