import { describe, expect, it } from "vitest";

import type { GraphEdge, GraphNode, WorkflowGraph } from "@/api/studio-types";
import type { GlyphFill } from "@/components/marks/node-glyph";
import { render } from "@/shared/test-support/render";

import { MiniGraph } from "./mini-graph";

// The strip's own three jobs: it drops params from the row, orders the rest by the wiring rather
// than the array order the backend happened to send, and tints each node and edge off the run's
// own progress -- including the fade that follows a failure downstream.

function node(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    key: overrides.id ?? "n",
    id: "n",
    kind: "run",
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

function edge(from: string, to: string, kind: GraphEdge["kind"] = "wire"): GraphEdge {
  return { from, to, kind, label: "" };
}

function graphFixture(nodes: GraphNode[], edges: GraphEdge[]): WorkflowGraph {
  return { name: "wf", version: 1, nodes, edges };
}

describe("MiniGraph", () => {
  it("renders nothing while the graph has not loaded", () => {
    const { container } = render(<MiniGraph graph={undefined} fillFor={() => "not-run"} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("drops param nodes from the strip, keeping only nodes with work", () => {
    const graph = graphFixture(
      [
        node({ key: "arg", id: "arg", kind: "param" }),
        node({ key: "draft", id: "draft", kind: "run" }),
      ],
      [edge("arg", "draft")],
    );
    const { container } = render(<MiniGraph graph={graph} fillFor={() => "ran"} />);

    const labels = Array.from(container.querySelectorAll("span.font-mono")).map(
      (el) => el.textContent,
    );
    expect(labels).toEqual(["draft"]);
  });

  it("orders node groups by the wiring, not by the array's own order", () => {
    const graph = graphFixture(
      [node({ key: "b", id: "b" }), node({ key: "a", id: "a" }), node({ key: "c", id: "c" })],
      [edge("a", "b"), edge("b", "c")],
    );
    const { container } = render(<MiniGraph graph={graph} fillFor={() => "ran"} />);

    const labels = Array.from(container.querySelectorAll("span.font-mono")).map(
      (el) => el.textContent,
    );
    expect(labels).toEqual(["a", "b", "c"]);
  });

  it("never follows a rejection back edge into a cycle", () => {
    const graph = graphFixture(
      [node({ key: "compose", id: "compose" }), node({ key: "gate", id: "gate", kind: "gate" })],
      [edge("compose", "gate"), edge("gate", "compose", "rejected")],
    );
    const { container } = render(<MiniGraph graph={graph} fillFor={() => "ran"} />);

    const labels = Array.from(container.querySelectorAll("span.font-mono")).map(
      (el) => el.textContent,
    );
    expect(labels).toEqual(["compose", "gate"]);
  });

  it("draws a gate node as the person gate shape and a plain step as the step shape", () => {
    const graph = graphFixture(
      [
        node({ key: "draft", id: "draft", kind: "run" }),
        node({ key: "review", id: "review", kind: "gate" }),
      ],
      [edge("draft", "review")],
    );
    const { container } = render(<MiniGraph graph={graph} fillFor={() => "ran"} />);

    const glyphs = container.querySelectorAll("svg[data-glyph-kind]");
    expect(Array.from(glyphs).map((svg) => svg.getAttribute("data-glyph-kind"))).toEqual([
      "step",
      "person-gate",
    ]);
  });

  it("tints the edge cobalt when it leads into the active node, ink when both ends have run", () => {
    const graph = graphFixture(
      [node({ key: "a", id: "a" }), node({ key: "b", id: "b" }), node({ key: "c", id: "c" })],
      [edge("a", "b"), edge("b", "c")],
    );
    const fillOf: Record<string, GlyphFill> = { a: "ran", b: "running", c: "not-run" };
    const { container } = render(
      <MiniGraph graph={graph} fillFor={(n) => fillOf[n.id] ?? "not-run"} />,
    );

    const edges = container.querySelectorAll("[data-edge-tone]");
    expect(edges[0]).toHaveClass("bg-accent"); // a -> b, leads into the running node
    expect(edges[1]).toHaveClass("bg-line"); // b -> c, c has not been reached
  });

  it("fades every node group and edge past a failed node, leaving the failed node itself full strength", () => {
    const graph = graphFixture(
      [node({ key: "a", id: "a" }), node({ key: "b", id: "b" }), node({ key: "c", id: "c" })],
      [edge("a", "b"), edge("b", "c")],
    );
    const fillOf: Record<string, GlyphFill> = { a: "ran", b: "failed", c: "not-run" };
    const { container } = render(
      <MiniGraph graph={graph} fillFor={(n) => fillOf[n.id] ?? "not-run"} />,
    );

    const groups = container.querySelectorAll("div.flex.w-16");
    expect(groups[0]).not.toHaveClass("opacity-50"); // a, before the failure
    expect(groups[1]).not.toHaveClass("opacity-50"); // b, the failed node itself
    expect(groups[2]).toHaveClass("opacity-50"); // c, past the failure

    const edges = container.querySelectorAll("[data-edge-tone]");
    expect(edges[0]).not.toHaveClass("opacity-50"); // a -> b
    expect(edges[1]).toHaveClass("opacity-50"); // b -> c, past the failure
  });
});
