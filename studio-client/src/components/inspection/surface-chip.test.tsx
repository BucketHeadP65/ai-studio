import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { SurfaceChip } from "./surface-chip";

// Where a fix lands, shown as one chip: the surface kind in words, and its own name in mono when it has one.

describe("SurfaceChip", () => {
  it("shows the surface kind in words and the surface name in mono", () => {
    render(<SurfaceChip blame={{ surface_kind: "tool", surface_name: "fetch_workflow_spec" }} />);

    expect(screen.getByText("tool")).toBeInTheDocument();
    expect(screen.getByText("fetch_workflow_spec")).toBeInTheDocument();
  });

  it("shows only the kind in words, exactly, when the surface has no name", () => {
    const { container } = render(<SurfaceChip blame={{ surface_kind: "model_tier", surface_name: "" }} />);

    expect(container).toHaveTextContent(/^model tier$/);
  });
});
