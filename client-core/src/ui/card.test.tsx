import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Card } from "./card";

describe("<Card />", () => {
  it("renders children on the shared surface classes", () => {
    render(<Card data-testid="card">inside</Card>);

    const card = screen.getByTestId("card");
    expect(card).toHaveTextContent("inside");
    expect(card.className).toMatch(/bg-surface/);
  });

  it("merges a caller's className over the defaults", () => {
    render(
      <Card data-testid="card" className="p-4">
        x
      </Card>,
    );

    expect(screen.getByTestId("card").className).toMatch(/p-4/);
  });
});
