import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusDot } from "./status-dot";

describe("<StatusDot />", () => {
  it("is decorative: hidden from assistive tech", () => {
    const { container } = render(<StatusDot tone="ok" />);

    expect(container.querySelector("span")).toHaveAttribute("aria-hidden");
  });

  it("pulses only when asked", () => {
    const { container: still } = render(<StatusDot tone="accent" />);
    const { container: busy } = render(<StatusDot tone="accent" pulse />);

    expect(still.querySelector("span")?.className).not.toMatch(/animate-pulse/);
    expect(busy.querySelector("span")?.className).toMatch(/animate-pulse/);
  });
});
