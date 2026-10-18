import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { CollapsibleCard } from "./collapsible-card";

// The one folding card every section of a page shares: open by default, the title is the
// toggle, the body leaves the document while closed, and the aside stays outside the toggle.

describe("CollapsibleCard", () => {
  it("opens by default, then hides its body when the title is pressed", async () => {
    render(
      <CollapsibleCard title="What it does">
        <p>the body</p>
      </CollapsibleCard>,
    );

    const toggle = screen.getByRole("button", { name: "What it does" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("the body")).toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("the body")).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(screen.getByText("the body")).toBeInTheDocument();
  });

  it("starts closed when asked to", () => {
    render(
      <CollapsibleCard title="The map" defaultOpen={false}>
        <p>the body</p>
      </CollapsibleCard>,
    );

    expect(screen.getByRole("button", { name: "The map" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("the body")).not.toBeInTheDocument();
  });

  it("keeps the aside outside the toggle, so a control there never nests in it", async () => {
    render(
      <CollapsibleCard
        title="What it takes"
        aside={<button type="button">Approve it</button>}
        id="takes"
      >
        <p>the body</p>
      </CollapsibleCard>,
    );

    const toggle = screen.getByRole("button", { name: "What it takes" });
    const asideButton = screen.getByRole("button", { name: "Approve it" });
    expect(toggle.contains(asideButton)).toBe(false);
    expect(document.getElementById("takes")).not.toBeNull();

    await userEvent.click(asideButton);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });
});
