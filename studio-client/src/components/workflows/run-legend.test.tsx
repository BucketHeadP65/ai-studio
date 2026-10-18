import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { RunLegend } from "./run-legend";

// The static key under the run cards: every swatch word, plus the explainer sentence.

describe("RunLegend", () => {
  it("names all four swatches and the explainer sentence", () => {
    render(<RunLegend />);

    expect(screen.getByText("How the drawing reads")).toBeInTheDocument();
    expect(screen.getByText("Step")).toBeInTheDocument();
    expect(screen.getByText("Gate a person owns")).toBeInTheDocument();
    expect(screen.getByText("Gate the agent owns")).toBeInTheDocument();
    expect(screen.getByText("Not run yet")).toBeInTheDocument();
    expect(
      screen.getByText("Shape says what type of step it is, fill says whether it ran."),
    ).toBeInTheDocument();
  });
});
