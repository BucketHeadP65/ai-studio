import { fireEvent, screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { WorkflowGraph } from "@/api/studio-types";
import { render } from "@/shared/test-support/render";

const routerBack = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ back: routerBack }),
}));

// Imported after the mock so the page sees the mocked module.
import { InspectorPage } from "./inspector-page";

// The mandate this shell exists to satisfy: the main column and the side column both readable
// in one render, no tab, no toggle, nothing hidden behind a click before it can even be checked
// for. The main column takes one of two shapes -- the plain canvas-hero every caller but the
// run detail page still uses (`graph` plus an optional `footer`), or `body`, the run detail
// page's own stacked cards, which replaces the hero entirely when given.

const GRAPH: WorkflowGraph = {
  name: "lit-check",
  version: 1,
  nodes: [
    {
      key: "draft",
      id: "draft",
      kind: "run",
      signature: "compose() -> str",
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

describe("InspectorPage", () => {
  it("shows the title, the canvas, the timeline footer, and the side column together", async () => {
    const { container } = render(
      <InspectorPage
        title="lit-check v1"
        graph={GRAPH}
        highlight={undefined}
        side={<p>the side column</p>}
        footer={<p>the timeline footer</p>}
      />,
    );

    expect(screen.getByText("lit-check v1")).toBeInTheDocument();
    expect(screen.getByText("the timeline footer")).toBeInTheDocument();
    expect(screen.getByText("the side column")).toBeInTheDocument();
    await waitFor(() => {
      if (!container.querySelector('[data-node="draft"]')) throw new Error("graph not drawn yet");
    });
  });

  it("renders with no graph or footer at all when the caller has none to show", () => {
    render(<InspectorPage title="a draft" side={<p>the side column</p>} />);

    expect(screen.getByText("a draft")).toBeInTheDocument();
    expect(screen.getByText("the side column")).toBeInTheDocument();
  });

  it("renders the caller's own body instead of the canvas hero when given one", () => {
    const { container } = render(
      <InspectorPage
        title="a run"
        body={<p>the run detail stack</p>}
        graph={GRAPH}
        side={<p>the side column</p>}
      />,
    );

    expect(screen.getByText("the run detail stack")).toBeInTheDocument();
    expect(screen.getByText("the side column")).toBeInTheDocument();
    expect(container.querySelector('[data-node="draft"]')).not.toBeInTheDocument();
  });

  it("goes back on Back", async () => {
    render(
      <InspectorPage
        title="lit-check v1"
        graph={GRAPH}
        highlight={undefined}
        side={<p>the side column</p>}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(routerBack).toHaveBeenCalledTimes(1);
  });

  it("opens a node on click", async () => {
    const onNodeClick = vi.fn();
    const { container } = render(
      <InspectorPage
        title="lit-check v1"
        graph={GRAPH}
        highlight={undefined}
        side={<p>the side column</p>}
        onNodeClick={onNodeClick}
      />,
    );

    const card = await waitFor(() => {
      const found = container.querySelector('[data-node="draft"]');
      if (!found) throw new Error("draft card not drawn yet");
      return found;
    });
    fireEvent.click(card);

    expect(onNodeClick).toHaveBeenCalledWith(GRAPH.nodes[0]);
  });
});
