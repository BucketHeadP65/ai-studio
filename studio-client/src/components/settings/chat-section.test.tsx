import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { ChatSection } from "./chat-section";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

describe("<ChatSection />", () => {
  it("offers the two ways to treat a settled turn's earlier steps, keeping them open by default", () => {
    render(<ChatSection />);

    expect(screen.getByRole("group", { name: "Earlier steps" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Keep open" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Fold after the answer" })).not.toBeChecked();
  });

  it("remembers the choice for the next visit", async () => {
    const user = userEvent.setup();
    const first = render(<ChatSection />);

    await user.click(screen.getByRole("radio", { name: "Fold after the answer" }));
    first.unmount();
    render(<ChatSection />);

    expect(await screen.findByRole("radio", { name: "Fold after the answer" })).toBeChecked();
  });
});
