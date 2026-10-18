import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ThinkingMark } from "./thinking-mark";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ThinkingMark", () => {
  it("reads as a status named Thinking with a verb and no details when it knows nothing", () => {
    render(<ThinkingMark startedAt={null} tokensOut={null} />);

    const mark = screen.getByRole("status", { name: "Thinking" });
    expect(mark.textContent).not.toContain("…");
    expect(mark.textContent).not.toContain("(");
  });

  it("shows how long the turn has run and the tokens it spent", () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-29T10:01:02.000Z"));
    render(<ThinkingMark startedAt="2026-08-29T10:00:00.000Z" tokensOut={3900} />);

    expect(screen.getByRole("status", { name: "Thinking" }).textContent).toContain(
      "(1:02, ↓ 3.9k tokens)",
    );
  });

  it("moves the verb on as the seconds pass", () => {
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-29T10:00:00.000Z"));
    const { rerender } = render(
      <ThinkingMark startedAt="2026-08-29T10:00:00.000Z" tokensOut={null} />,
    );
    const verb = () => screen.getByRole("status").textContent?.match(/^\w+/)?.[0];
    const first = verb();

    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-29T10:00:07.000Z"));
    rerender(<ThinkingMark startedAt="2026-08-29T09:59:53.000Z" tokensOut={null} />);

    expect(verb()).not.toBe(first);
  });

  it("marks the spinning glyph and the pulsing verb essential, so reduced motion spares them", () => {
    const { container } = render(
      <ThinkingMark startedAt="2026-08-29T10:00:00.000Z" tokensOut={null} />,
    );

    expect(container.querySelector("svg")).toHaveAttribute("data-motion", "essential");
    expect(screen.getByText(/^\w+$/)).toHaveAttribute("data-motion", "essential");
  });
});
