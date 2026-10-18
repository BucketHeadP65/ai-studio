import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import type { GraphNode, WorkflowGraph } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { StepSpine } from "./step-spine";

// The card reading a workflow's own steps, one row per node from `executionOrder`, a run or map
// row's own shape (Takes, Tools, The briefing, The program, Produces) against a gate row's own
// shape (Reviews, The charge, Rejected, Passes on) under the shared head every row carries.

function node(overrides: Partial<GraphNode> & Pick<GraphNode, "id" | "kind">): GraphNode {
  return {
    key: overrides.id,
    signature: "",
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

// A sentence's own words sit across several inline spans (a mono id, a mono type), so the exact
// text is never one element's own direct text node -- match the full, rendered sentence instead.
function sentence(text: string) {
  return (_content: string, element: Element | null) => element?.textContent === text;
}

const DIGEST_RESULT = {
  type: "object" as const,
  title: "DigestResult",
  required: ["digest_id", "status"],
  properties: {
    status: { enum: ["written", "skipped"], type: "string" },
    digest_id: { type: "string" },
  },
};

const RESEARCH_GRAPH: WorkflowGraph = {
  name: "research-digest",
  version: 1,
  nodes: [
    node({
      key: "param:collection",
      id: "collection",
      kind: "param",
      signature: "string",
      detail: "the knowledge base to draw from",
    }),
    node({
      id: "undigested",
      kind: "run",
      detail: "Lists items not yet digested.",
      brought: false,
      work: "list_undigested",
      output_schema: { type: "array", items: { type: "object", title: "Undigested" } },
    }),
    node({
      id: "digests",
      kind: "map",
      detail: "Digests each undigested item.",
      tools: ["read_source", "write_note"],
      brought: true,
      briefing: "Read the whole mirror with read_source, passing the collection as knowledge_base.",
      work: "digest_source",
      input_schemas: { source: { type: "object", title: "Undigested" } },
      output_schema: { type: "array", items: DIGEST_RESULT },
    }),
    node({
      id: "report",
      kind: "run",
      detail: "Composes the final report.",
      is_output: true,
      work: "compose_report",
      input_schemas: { digests: { type: "array", items: DIGEST_RESULT } },
    }),
  ],
  edges: [
    { from: "param:collection", to: "undigested", kind: "wire", label: "collection: string" },
    { from: "undigested", to: "digests", kind: "map", label: "each source: DigestResult" },
    { from: "digests", to: "report", kind: "wire", label: "digests: list[DigestResult]" },
  ],
};

const RELATE_RESULT = {
  type: "object" as const,
  title: "RelateResult",
  required: ["relation_id"],
  properties: { relation_id: { type: "string" } },
};

// The same map node as RESEARCH_GRAPH's "digests", now chaining "relate" on each digest --
// RESEARCH_GRAPH itself stays untouched, so every existing test above still renders a plain map.
const CHAINED_RESEARCH_GRAPH: WorkflowGraph = {
  ...RESEARCH_GRAPH,
  nodes: RESEARCH_GRAPH.nodes.map((n) =>
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
            {
              work: "relate",
              node_id: "digests.relate",
              briefing: "Draw one relation between this digest and what it cites.",
              tools: ["write_relation"],
              output_schema: RELATE_RESULT,
              source: "",
            },
          ],
        }
      : n,
  ),
};

// The same chained map, with a third turn -- so a reader sees a map that fans out to two extra
// turns after the first, not just one.
const THREE_TURN_GRAPH: WorkflowGraph = {
  ...CHAINED_RESEARCH_GRAPH,
  nodes: CHAINED_RESEARCH_GRAPH.nodes.map((n) =>
    n.id === "digests" && n.turns
      ? {
          ...n,
          turns: [
            ...n.turns,
            {
              work: "extract_references",
              node_id: "digests.extract_references",
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

describe("StepSpine, a map's chained leaf", () => {
  it("shows the chained work, its briefing, tools, and schema inside the same map row", () => {
    render(<StepSpine graph={CHAINED_RESEARCH_GRAPH} />);

    expect(screen.getByText("Turn 2, for each result")).toBeInTheDocument();
    expect(screen.getByText("relate")).toBeInTheDocument();
    expect(
      screen.getByText("Draw one relation between this digest and what it cites."),
    ).toBeInTheDocument();
    expect(screen.getByText("write_relation")).toBeInTheDocument();
    expect(screen.getByText("One RelateResult with these fields")).toBeInTheDocument();

    // still one row for the map node, not a stage of its own
    const ids = screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent);
    expect(ids).toEqual(["undigested", "digests", "report"]);
  });

  it("renders a plain map row with none of the chained UI", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.queryByText("Turn 2, for each result")).not.toBeInTheDocument();
    expect(screen.queryByText("write_relation")).not.toBeInTheDocument();
  });

  it("numbers each extra turn by its position, so a three-turn map never repeats a label", () => {
    render(<StepSpine graph={THREE_TURN_GRAPH} />);

    expect(screen.getByText("Turn 2, for each result")).toBeInTheDocument();
    expect(screen.getByText("Turn 3, for each result")).toBeInTheDocument();
    expect(screen.queryByText("Then, for each result")).not.toBeInTheDocument();
    expect(screen.getByText("extract_references")).toBeInTheDocument();
  });
});

describe("StepSpine, run and map rows", () => {
  it("renders one row per non param node, in execution order", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("The steps")).toBeInTheDocument();
    const ids = screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent);
    expect(ids).toEqual(["undigested", "digests", "report"]);
  });

  it("counts the stages and says one after another when no stage fans out", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("3 stages, one after another")).toBeInTheDocument();
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getAllByText("Then")).toHaveLength(2);
  });

  it("labels a stage whose nodes fan out, so nobody reads the spine as sequential", () => {
    const graph: WorkflowGraph = {
      ...RESEARCH_GRAPH,
      nodes: [
        ...RESEARCH_GRAPH.nodes,
        node({ id: "references", kind: "map", work: "find_references" }),
      ],
      edges: [
        ...RESEARCH_GRAPH.edges,
        { from: "undigested", to: "references", kind: "map", label: "each source: Reference" },
      ],
    };
    render(<StepSpine graph={graph} />);

    expect(screen.getByText("Then, these 2 run side by side")).toBeInTheDocument();
    const ids = screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent);
    expect(ids).toEqual(["undigested", "digests", "references", "report"]);
  });

  it("says the two nodes of the first parallel level run side by side", () => {
    const graph: WorkflowGraph = {
      ...RESEARCH_GRAPH,
      nodes: [
        ...RESEARCH_GRAPH.nodes,
        node({ id: "references", kind: "map", work: "find_references" }),
      ],
      edges: [
        ...RESEARCH_GRAPH.edges,
        { from: "undigested", to: "references", kind: "map", label: "each source: Reference" },
      ],
    };
    render(<StepSpine graph={graph} />);

    expect(
      screen.getAllByText(sentence("3 stages, digests and references run side by side")).length,
    ).toBeGreaterThan(0);
  });

  it("reads the map row's own kind sentence, wrapped for running once per item", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(
      screen.getByText(
        sentence(
          "Briefs an agent, digest_source, once for every item of undigested, side by side.",
        ),
      ),
    ).toBeInTheDocument();
  });

  it("reads a run row's own kind sentence off brought false, no briefing, no program", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(
      screen.getByText(sentence("Runs a step this app provides, compose_report.")),
    ).toBeInTheDocument();
  });

  it("tags a carried node and the run's own result", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("carried")).toBeInTheDocument();
    expect(screen.getByText("The run's result")).toBeInTheDocument();
  });

  it("shows Missing here for an unknown step, and never its backend detail saying the same", () => {
    const graph: WorkflowGraph = {
      ...RESEARCH_GRAPH,
      nodes: RESEARCH_GRAPH.nodes.map((n) =>
        n.id === "report"
          ? { ...n, unknown: true, detail: "This app has no step called 'compose_report'." }
          : n,
      ),
    };
    render(<StepSpine graph={graph} />);

    expect(screen.getByText("Missing here")).toBeInTheDocument();
    expect(
      screen.getByText(sentence("This app has no step called compose_report.")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("This app has no step called 'compose_report'."),
    ).not.toBeInTheDocument();
  });

  it("omits Takes for a step that takes nothing", () => {
    const graph: WorkflowGraph = {
      name: "no-inputs",
      version: 1,
      nodes: [node({ id: "tick", kind: "run", work: "tick", is_output: true })],
      edges: [],
    };
    render(<StepSpine graph={graph} />);

    expect(screen.queryByText("Takes")).not.toBeInTheDocument();
  });

  it("shows a provided step's own detail, never a carried step's or a map's generated one", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("Lists items not yet digested.")).toBeInTheDocument();
    expect(screen.getByText("Composes the final report.")).toBeInTheDocument();
    expect(screen.queryByText("Digests each undigested item.")).not.toBeInTheDocument();
  });

  it("reads a Takes line's own type off input_schemas, in a mono span", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("list of DigestResult")).toBeInTheDocument();
  });

  it("reads a Takes line naming the source parameter, when the wire comes from one", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("from the parameter")).toBeInTheDocument();
  });

  it("reads a map row's each Takes line as one item of the node it maps over", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText(/each item of/)).toBeInTheDocument();
  });

  it("shows Tools as chips only when the node carries any", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("Tools")).toBeInTheDocument();
    expect(screen.getByText("read_source")).toBeInTheDocument();
    expect(screen.getByText("write_note")).toBeInTheDocument();
  });

  it("shows The briefing as a quote block when the node carries one", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getByText("The briefing")).toBeInTheDocument();
    expect(
      screen.getByText("Read the whole mirror with read_source, passing the collection as knowledge_base."),
    ).toBeInTheDocument();
  });

  it("omits The briefing and The program for a node that carries neither", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    // Only `digests` carries a briefing, and no node here carries a program.
    expect(screen.getAllByText("The briefing")).toHaveLength(1);
    expect(screen.queryByText("The program")).not.toBeInTheDocument();
  });

  it("shows The program as a dark mono block when the node carries a program", () => {
    const graph: WorkflowGraph = {
      ...RESEARCH_GRAPH,
      nodes: RESEARCH_GRAPH.nodes.map((n) =>
        n.id === "report" ? { ...n, source: "print('hi')" } : n,
      ),
    };
    render(<StepSpine graph={graph} />);

    expect(screen.getByText("The program")).toBeInTheDocument();
    expect(screen.getByText("print('hi')")).toBeInTheDocument();
  });

  it("renders Produces as the node's own output schema", () => {
    render(<StepSpine graph={RESEARCH_GRAPH} />);

    expect(screen.getAllByText("Produces").length).toBe(3);
    expect(screen.getByText("A list of DigestResult, each with these fields")).toBeInTheDocument();
  });
});

describe("StepSpine, gate rows", () => {
  const GATE_GRAPH: WorkflowGraph = {
    name: "gated",
    version: 1,
    nodes: [
      node({ id: "compose", kind: "run", work: "compose", output_schema: { type: "string" } }),
      node({ id: "approve", kind: "gate", output_schema: { type: "string" } }),
    ],
    edges: [
      { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
      { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
    ],
  };

  it("reads a person gate's own kind sentence and composed description", () => {
    render(<StepSpine graph={GATE_GRAPH} />);

    expect(screen.getByText("A person approves.")).toBeInTheDocument();
    expect(
      screen.getByText(
        sentence(
          "The run stops here until a person approves or rejects what compose produced. Approved, it passes through unchanged. Rejected, the run goes back to compose and runs again from there.",
        ),
      ),
    ).toBeInTheDocument();
  });

  it("shows Reviews, Rejected, and Passes on for the gate row, never Takes or Tools there", () => {
    render(<StepSpine graph={GATE_GRAPH} />);

    const gateRow = screen.getByText("Reviews").closest('[class*="grid-cols"]');
    expect(gateRow).not.toBeNull();
    expect(gateRow).toHaveTextContent("Rejected");
    expect(gateRow).toHaveTextContent("Passes on");
    expect(gateRow).not.toHaveTextContent("Takes");
    expect(gateRow).not.toHaveTextContent("Tools");
  });

  it("says the same value, as the run's result, when the gate is the output with nothing downstream", () => {
    const graph: WorkflowGraph = {
      ...GATE_GRAPH,
      nodes: GATE_GRAPH.nodes.map((n) => (n.id === "approve" ? { ...n, is_output: true } : n)),
    };
    render(<StepSpine graph={graph} />);

    expect(screen.getByText("the same value, as the run's result")).toBeInTheDocument();
  });

  it("reads the orchestrator gate's own kind sentence, description, and its charge quote block", () => {
    const graph: WorkflowGraph = {
      name: "orchestrated",
      version: 1,
      nodes: [
        node({ id: "compose", kind: "run", work: "compose" }),
        node({
          id: "approve",
          kind: "orchestrator-gate",
          briefing: "Reject anything under 3 sources.",
        }),
      ],
      edges: [
        { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
        { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
      ],
    };
    render(<StepSpine graph={graph} />);

    expect(
      screen.getByText("The orchestrator decides, no person approves it."),
    ).toBeInTheDocument();
    expect(screen.getByText("The charge")).toBeInTheDocument();
    expect(screen.getByText("Reject anything under 3 sources.")).toBeInTheDocument();
    expect(
      screen.getByText(
        sentence(
          "This run's orchestrator judges what compose produced against the charge below. Approved, it passes through unchanged. Rejected, the run goes back to compose and runs again from there.",
        ),
      ),
    ).toBeInTheDocument();
  });

  it("reads a judge's own kind sentence and description, naming its own work", () => {
    const graph: WorkflowGraph = {
      name: "judged",
      version: 1,
      nodes: [
        node({ id: "compose", kind: "run", work: "compose" }),
        node({ id: "check", kind: "judge", work: "quality_check" }),
      ],
      edges: [
        { from: "compose", to: "check", kind: "wire", label: "reviews: string" },
        { from: "check", to: "compose", kind: "rejected", label: "rejected" },
      ],
    };
    render(<StepSpine graph={graph} />);

    expect(
      screen.getByText(sentence("A step decides, quality_check, no person approves it.")),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        sentence(
          "The step quality_check judges what compose produced. Approved, it passes through unchanged. Rejected, the run goes back to compose and runs again from there.",
        ),
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("The charge")).not.toBeInTheDocument();
  });
});
