import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { Folded } from "./folded";

describe("Folded", () => {
  it("shows short text with no fold button", () => {
    render(<Folded text="one line" />);

    expect(screen.getByText("one line")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("folds text past its line count, then unfolds on the button", async () => {
    const lines = Array.from({ length: 25 }, (_, index) => `line ${index}`);
    const text = lines.join("\n");
    const { container } = render(<Folded text={text} lines={20} />);

    expect(screen.getByText("Show all (25 lines)")).toBeInTheDocument();
    expect(container.textContent).toContain("line 0");
    expect(container.textContent).not.toContain("line 24");

    await userEvent.click(screen.getByRole("button", { name: "Show all (25 lines)" }));

    expect(screen.getByText("Show the first 20 lines")).toBeInTheDocument();
    expect(container.textContent).toContain("line 24");
  });

  it("clips the box with a fade while folded, dropping the fade once opened", async () => {
    const lines = Array.from({ length: 25 }, (_, index) => `line ${index}`);
    const text = lines.join("\n");
    const { container } = render(<Folded text={text} lines={20} fade />);

    const pre = container.querySelector("pre") as HTMLElement;
    expect(pre.className).toContain("overflow-hidden");

    await userEvent.click(screen.getByRole("button", { name: "Show all (25 lines)" }));

    expect(pre.className).not.toContain("overflow-hidden");
  });
});
