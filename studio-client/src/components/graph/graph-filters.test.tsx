import type { GraphResult } from "@/api/types";
import { render } from "@/test-support/render";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GraphFilters, pageTypeOptions, relOptions } from "./graph-filters";

afterEach(cleanup);

const graph: GraphResult = {
  nodes: [
    { id: "p1", type: "paper", title: "P1", status: "synthesized", superseded: false },
    { id: "c1", type: "concept", title: "C1", status: "draft", superseded: false },
    { id: "p2", type: "paper", title: "P2", status: "synthesized", superseded: false },
  ],
  edges: [
    { src_id: "p1", dst_id: "c1", rel: "introduces", provenance_claim: null },
    { src_id: "p2", dst_id: "c1", rel: "builds_on", provenance_claim: null },
    { src_id: "p1", dst_id: "p2", rel: "builds_on", provenance_claim: null },
  ],
};

describe("option derivation", () => {
  it("derives distinct, sorted page types from nodes", () => {
    expect(pageTypeOptions(graph)).toEqual(["concept", "paper"]);
  });
  it("derives distinct, sorted rels from edges", () => {
    expect(relOptions(graph)).toEqual(["builds_on", "introduces"]);
  });
});

describe("GraphFilters", () => {
  it("renders both selects with an 'All' default plus the derived options", () => {
    render(<GraphFilters graph={graph} value={{}} onChange={vi.fn()} />);
    const pageType = screen.getByLabelText("Page type") as HTMLSelectElement;
    const rel = screen.getByLabelText("Relation") as HTMLSelectElement;
    expect([...pageType.options].map((o) => o.textContent)).toEqual(["All", "concept", "paper"]);
    expect([...rel.options].map((o) => o.textContent)).toEqual(["All", "builds_on", "introduces"]);
    expect(pageType.value).toBe(""); // All
    expect(rel.value).toBe("");
  });

  it("propagates a page_type selection upward", async () => {
    const onChange = vi.fn();
    render(<GraphFilters graph={graph} value={{}} onChange={onChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Page type"), "concept");
    expect(onChange).toHaveBeenCalledWith({ page_type: "concept" });
  });

  it("propagates a rel selection upward", async () => {
    const onChange = vi.fn();
    render(<GraphFilters graph={graph} value={{}} onChange={onChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Relation"), "builds_on");
    expect(onChange).toHaveBeenCalledWith({ rel: "builds_on" });
  });

  it("maps the 'All' option back to undefined (clears the filter)", async () => {
    const onChange = vi.fn();
    render(<GraphFilters graph={graph} value={{ page_type: "concept" }} onChange={onChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Page type"), "All");
    expect(onChange).toHaveBeenCalledWith({ page_type: undefined });
  });

  it("reflects the controlled value", () => {
    render(
      <GraphFilters
        graph={graph}
        value={{ page_type: "paper", rel: "builds_on" }}
        onChange={vi.fn()}
      />,
    );
    expect((screen.getByLabelText("Page type") as HTMLSelectElement).value).toBe("paper");
    expect((screen.getByLabelText("Relation") as HTMLSelectElement).value).toBe("builds_on");
  });
});
