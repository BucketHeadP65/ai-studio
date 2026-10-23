import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { Rail } from "./rail";

// The IDE-drawer behaviour: open, a rail shows its content, a collapse control,
// and a resize separator; collapsed, it shrinks to a labelled strip that can
// reopen it, never `display: none` with no way back.

describe("Rail", () => {
  it("shows content, a collapse control and a resize handle when open", async () => {
    const onToggle = vi.fn();
    render(
      <Rail side="right" title="Workflows" storageKey="test-rail" open onToggle={onToggle}>
        <p>the shelf</p>
      </Rail>,
    );
    expect(screen.getByText("the shelf")).toBeInTheDocument();
    expect(screen.getByRole("separator")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Collapse Workflows" }));
    expect(onToggle).toHaveBeenCalledWith(false);
  });

  it("names its region with a level-two heading, under the station's own", () => {
    render(
      <Rail side="right" title="Workflows" storageKey="test-rail" open onToggle={vi.fn()}>
        <p>the shelf</p>
      </Rail>,
    );

    // A reader moving by headings meets the station's h1 and then this, with nothing skipped.
    expect(screen.getByRole("heading", { level: 2, name: "Workflows" })).toBeInTheDocument();
  });

  it("collapses to a strip that reopens it", async () => {
    const onToggle = vi.fn();
    render(
      <Rail side="right" title="Workflows" storageKey="test-rail" open={false} onToggle={onToggle}>
        <p>the shelf</p>
      </Rail>,
    );
    expect(screen.queryByText("the shelf")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open Workflows" }));
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("shows the page's marker on the folded strip and nothing when there is none", () => {
    const { unmount } = render(
      <Rail
        side="right"
        title="Workflows"
        storageKey="test-rail"
        open={false}
        onToggle={vi.fn()}
        marker={<span role="img" aria-label="A run is live" />}
      >
        <p>the shelf</p>
      </Rail>,
    );
    expect(screen.getByRole("img", { name: "A run is live" })).toBeInTheDocument();
    unmount();

    render(
      <Rail side="right" title="Workflows" storageKey="test-rail" open={false} onToggle={vi.fn()}>
        <p>the shelf</p>
      </Rail>,
    );
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
