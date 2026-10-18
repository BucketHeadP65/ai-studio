import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmptyState } from "./empty-state";

describe("<EmptyState />", () => {
  it("says what the empty place is for", () => {
    render(<EmptyState title="Nothing authored yet" hint="Ask the agent to craft one." />);

    expect(screen.getByText("Nothing authored yet")).toBeInTheDocument();
    expect(screen.getByText("Ask the agent to craft one.")).toBeInTheDocument();
  });

  it("carries the first action as children", () => {
    render(
      <EmptyState title="No corpora">
        <button type="button">New corpus</button>
      </EmptyState>,
    );

    expect(screen.getByRole("button", { name: "New corpus" })).toBeInTheDocument();
  });
});
