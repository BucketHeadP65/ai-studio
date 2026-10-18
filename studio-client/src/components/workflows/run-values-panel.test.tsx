import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { GraphNode } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { RunValuesPanel } from "./run-values-panel";

function node(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
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
    ...overrides,
  };
}

describe("RunValuesPanel", () => {
  it("renders nothing while nothing has produced a value yet", () => {
    const { container } = render(
      <RunValuesPanel nodes={[]} values={null} onSelectNode={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("previews each node's own output without a click, in graph order", () => {
    const nodes = [node({ id: "intake" }), node({ id: "draft" })];
    render(
      <RunValuesPanel
        nodes={nodes}
        values={{ draft: "drafted output", intake: "intake output" }}
        onSelectNode={vi.fn()}
      />,
    );

    const rows = screen.getAllByRole("button");
    expect(rows[0]).toHaveTextContent("intake");
    expect(rows[0]).toHaveTextContent("intake output");
    expect(rows[1]).toHaveTextContent("draft");
    expect(rows[1]).toHaveTextContent("drafted output");
  });

  it("names the picked node to the caller on click", async () => {
    const onSelectNode = vi.fn();
    render(
      <RunValuesPanel
        nodes={[node({ id: "draft" })]}
        values={{ draft: "drafted output" }}
        onSelectNode={onSelectNode}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /draft/ }));

    expect(onSelectNode).toHaveBeenCalledWith("draft");
  });
});
