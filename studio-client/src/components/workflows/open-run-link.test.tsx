import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { OpenRunLink } from "./open-run-link";

describe("OpenRunLink", () => {
  it("links to the given href, named Open the run", () => {
    render(<OpenRunLink href="/workflows/wf.lit-check.v5" />);

    expect(screen.getByRole("link", { name: "Open the run" })).toHaveAttribute(
      "href",
      "/workflows/wf.lit-check.v5",
    );
  });

  it("carries a caller's layout classes alongside its own", () => {
    render(<OpenRunLink href="/workflows/wf.lit-check.v5" size="md" className="ml-auto" />);

    expect(screen.getByRole("link", { name: "Open the run" })).toHaveClass("ml-auto", "h-8");
  });
});
