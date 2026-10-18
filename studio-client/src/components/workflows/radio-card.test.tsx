import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/test-support/render";

import { RadioCardGroup } from "./radio-card";

type Mode = "a" | "b" | "c";

const OPTIONS = [
  { value: "a" as Mode, label: "Option A", description: "The first choice." },
  { value: "b" as Mode, label: "Option B", description: "The second choice." },
  { value: "c" as Mode, label: "Option C" },
];

describe("RadioCardGroup segmented", () => {
  const SEGMENTS = [
    { value: "a" as Mode, label: "Option A", short: "A", description: "The first choice." },
    { value: "b" as Mode, label: "Option B", short: "B", description: "The second choice." },
    { value: "c" as Mode, label: "Option C", short: "C", disabled: true, description: "Not now." },
  ];

  it("keeps a real radio per option, named by the label, showing the short word", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={SEGMENTS}
        variant="segmented"
      />,
    );

    expect(screen.getByRole("radio", { name: "Option A" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Option C" })).toBeDisabled();
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
  });

  it("shows the chosen option's description once and keeps the others readable to assistive tech", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={SEGMENTS}
        variant="segmented"
      />,
    );

    expect(screen.getAllByText("The first choice.")).toHaveLength(1);
    expect(screen.getByText("The first choice.")).not.toHaveClass("sr-only");
    expect(screen.getByText("The second choice.")).toHaveClass("sr-only");
    expect(screen.getByRole("radio", { name: "Option B" })).toHaveAccessibleDescription(
      "The second choice.",
    );
    expect(screen.getByRole("radio", { name: "Option A" })).toHaveAccessibleDescription(
      "The first choice.",
    );
  });

  it("fires onChange with the clicked segment's value", async () => {
    const onChange = vi.fn();
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={onChange}
        options={SEGMENTS}
        variant="segmented"
      />,
    );

    await userEvent.click(screen.getByRole("radio", { name: "Option B" }));

    expect(onChange).toHaveBeenCalledWith("b");
  });
});

describe("RadioCardGroup", () => {
  it("renders one real radio per option, marking the current value checked", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="b"
        onChange={vi.fn()}
        options={OPTIONS}
      />,
    );

    expect(screen.getByRole("radio", { name: "Option A" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Option B" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Option C" })).not.toBeChecked();
  });

  it("fires onChange with the clicked option's value", async () => {
    const onChange = vi.fn();
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={onChange}
        options={OPTIONS}
      />,
    );

    await userEvent.click(screen.getByRole("radio", { name: "Option C" }));

    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("shows a description under an option that has one, and none for an option that has none", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
      />,
    );

    expect(screen.getByText("The first choice.")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Option C" })).toBeInTheDocument();
  });

  it("keeps a description out of its radio's own accessible name", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
      />,
    );

    expect(screen.getByRole("radio", { name: "Option A" })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /first choice/ })).not.toBeInTheDocument();
  });

  it("disables one option without disabling the rest", async () => {
    const onChange = vi.fn();
    const disabledOptions = [OPTIONS[0], { ...OPTIONS[1], disabled: true }, OPTIONS[2]];
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={onChange}
        options={disabledOptions}
      />,
    );

    expect(screen.getByRole("radio", { name: "Option B" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Option A" })).not.toBeDisabled();
    await userEvent.click(screen.getByRole("radio", { name: "Option C" }));
    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("disables every option when the whole group is disabled", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
        disabled
      />,
    );

    for (const option of OPTIONS) {
      expect(screen.getByRole("radio", { name: option.label })).toBeDisabled();
    }
  });

  it("attaches the given ref to the first option's own input", () => {
    const ref = createRef<HTMLInputElement>();
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
        firstOptionRef={ref}
      />,
    );

    expect(ref.current).toBe(screen.getByRole("radio", { name: "Option A" }));
  });

  it("keeps every option reachable by keyboard, the focused card carrying a visible ring", async () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
      />,
    );

    await userEvent.tab();

    const first = screen.getByRole("radio", { name: "Option A" });
    expect(first).toHaveFocus();
    expect(first.closest("label")).toHaveClass("has-[:focus-visible]:ring-2");
  });

  it("uses only the house's allowed punctuation in the legend and every option", () => {
    render(
      <RadioCardGroup
        name="mode"
        legend="Pick one"
        value="a"
        onChange={vi.fn()}
        options={OPTIONS}
      />,
    );
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;

    expect(screen.getByText("Pick one").textContent ?? "").toMatch(ALLOWED_PUNCTUATION);
    for (const option of OPTIONS) {
      expect(
        screen.getByRole("radio", { name: option.label }).parentElement?.textContent ?? "",
      ).toMatch(ALLOWED_PUNCTUATION);
    }
  });
});
