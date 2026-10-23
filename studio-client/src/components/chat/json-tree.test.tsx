import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { JsonTree } from "./json-tree";

describe("JsonTree", () => {
  it("renders a primitive inline", () => {
    render(<JsonTree value={42} name="count" onView={vi.fn()} />);

    expect(screen.getByText("count")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("expands an object to depth 2 by default, then collapses on toggle", async () => {
    const user = userEvent.setup();
    render(<JsonTree value={{ a: { b: { c: 1 } } }} onView={vi.fn()} />);

    // depth 0 (the root) and depth 1 ("a") are expanded by default, exposing "b";
    // depth 2 ("b") is at the expand boundary and starts collapsed, so "c" is hidden.
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.queryByText("c")).not.toBeInTheDocument();

    await user.click(screen.getByText("b"));
    expect(screen.getByText("c")).toBeInTheDocument();

    await user.click(screen.getByText("b"));
    expect(screen.queryByText("c")).not.toBeInTheDocument();
  });

  it("renders an array with its length and indexed entries", () => {
    render(<JsonTree value={["x", "y"]} onView={vi.fn()} />);

    expect(screen.getByText("Array(2)")).toBeInTheDocument();
    expect(screen.getByText('"x"')).toBeInTheDocument();
    expect(screen.getByText('"y"')).toBeInTheDocument();
  });

  it("truncates a long string and fires onView with the full text", async () => {
    const user = userEvent.setup();
    const onView = vi.fn();
    const long = "a".repeat(300);
    render(<JsonTree value={long} onView={onView} />);

    expect(screen.getByText(`"${"a".repeat(240)}`, { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(long)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "View" }));

    expect(onView).toHaveBeenCalledWith(long);
  });

  it("leaves a short string untruncated, with no view affordance", () => {
    render(<JsonTree value="short text" onView={vi.fn()} />);

    expect(screen.getByText('"short text"')).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });
});
