import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { Highlighted } from "./highlighted";

describe("Highlighted", () => {
  it("wraps a matched phrase in a mark, the rest as plain text", () => {
    const { container } = render(
      <Highlighted text="fit: STALE: re-check before trusting it" phrases={["re-check before trusting it"]} />,
    );

    const mark = screen.getByText("re-check before trusting it");
    expect(mark.tagName).toBe("MARK");
    expect(container.querySelectorAll("mark")).toHaveLength(1);
    expect(container.textContent).toBe("fit: STALE: re-check before trusting it");
  });

  it("renders the text alone with no mark when there are no phrases", () => {
    const { container } = render(<Highlighted text="plain text here" phrases={[]} />);

    expect(container.textContent).toBe("plain text here");
    expect(container.querySelector("mark")).toBeNull();
  });
});
