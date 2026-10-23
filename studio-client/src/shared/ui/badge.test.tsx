import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge } from "./badge";

describe("<Badge />", () => {
  it("renders its children as the visible label", () => {
    render(<Badge>compiled</Badge>);

    expect(screen.getByText("compiled")).toBeInTheDocument();
  });

  it("defaults to the neutral tone", () => {
    render(<Badge>plain</Badge>);

    expect(screen.getByText("plain").className).toMatch(/muted-soft/);
  });

  it("tone='danger' tints with the danger pair", () => {
    render(<Badge tone="danger">broken</Badge>);

    expect(screen.getByText("broken").className).toMatch(/danger/);
  });
});
