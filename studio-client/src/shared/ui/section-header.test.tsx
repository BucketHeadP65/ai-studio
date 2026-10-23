import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SectionHeader } from "./section-header";

describe("<SectionHeader />", () => {
  it("renders the title as a heading", () => {
    render(<SectionHeader title="Workflows" />);

    expect(screen.getByRole("heading", { name: /workflows/i })).toBeInTheDocument();
  });

  it("shows the count beside the title when given", () => {
    render(<SectionHeader title="Agents" count={3} />);

    expect(screen.getByRole("heading", { name: /agents\s*\(3\)/i })).toBeInTheDocument();
  });

  it("renders the actions slot", () => {
    render(<SectionHeader title="Scripts" actions={<button type="button">New</button>} />);

    expect(screen.getByRole("button", { name: "New" })).toBeInTheDocument();
  });
});
