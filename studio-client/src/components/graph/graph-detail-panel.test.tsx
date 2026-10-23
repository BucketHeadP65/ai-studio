import type { GraphResult } from "@/api/types";
import { render } from "@/shared/test-support/render";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GraphDetailPanel } from "./graph-detail-panel";

afterEach(cleanup);

const graph: GraphResult = {
  nodes: [
    {
      id: "p1",
      type: "paper",
      title: "Dense Passage Retrieval",
      status: "synthesized",
      superseded: false,
    },
    { id: "p2", type: "concept", title: "Hybrid Search", status: "draft", superseded: true },
  ],
  edges: [{ src_id: "p1", dst_id: "p2", rel: "introduces", provenance_claim: "claim-7" }],
};

describe("GraphDetailPanel", () => {
  it("shows an empty hint when nothing is selected", () => {
    render(<GraphDetailPanel selection={null} graph={graph} />);
    expect(screen.getByText(/select a node or edge/i)).toBeInTheDocument();
  });

  it("renders a selected node's id, type, title, status, and superseded flag", () => {
    render(<GraphDetailPanel selection={{ node: graph.nodes[1] }} graph={graph} />);
    expect(screen.getByRole("heading", { name: "Hybrid Search" })).toBeInTheDocument();
    expect(screen.getByText("p2")).toBeInTheDocument();
    // Type, status and supersession are chips, each fact rendered ONCE, not
    // duplicated between a chip row and a field list.
    expect(screen.getByText("concept")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("superseded")).toBeInTheDocument();
  });

  it("renders a selected edge with resolved endpoint titles, rel, and provenance claim", () => {
    render(<GraphDetailPanel selection={{ edge: graph.edges[0] }} graph={graph} />);
    expect(screen.getByText("Dense Passage Retrieval")).toBeInTheDocument(); // src title
    expect(screen.getByText("Hybrid Search")).toBeInTheDocument(); // dst title
    expect(screen.getByText("claim-7")).toBeInTheDocument();
    // rel appears as heading
    expect(screen.getByRole("heading", { name: "introduces" })).toBeInTheDocument();
  });

  it("falls back to the raw id when an edge endpoint isn't in the loaded graph", () => {
    const edge = { src_id: "missing:x", dst_id: "p2", rel: "cites", provenance_claim: null };
    render(<GraphDetailPanel selection={{ edge }} graph={graph} />);
    expect(screen.getByText("missing:x")).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument(); // null provenance
  });

  // A detail panel that only DESCRIBES the selection is a dead end: reading a
  // graph means walking it, and the neighbours are the only edges the reader
  // can act on from here.
  it("lists the selected node's neighbours by relation and walks to one on click", async () => {
    const onSelectNode = vi.fn();
    render(
      <GraphDetailPanel
        selection={{ node: graph.nodes[0] }}
        graph={graph}
        onSelectNode={onSelectNode}
      />,
    );
    expect(screen.getByText(/introduces/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Hybrid Search/ }));
    expect(onSelectNode).toHaveBeenCalledWith("p2");
  });

  it("says so plainly when a node has no edges at all", () => {
    const lonely = {
      id: "p3",
      type: "meta",
      title: "Orphan",
      status: "stub",
      superseded: false,
    };
    render(
      <GraphDetailPanel
        selection={{ node: lonely }}
        graph={{ nodes: [...graph.nodes, lonely], edges: graph.edges }}
      />,
    );
    expect(screen.getByText(/no edges/i)).toBeInTheDocument();
  });
});
