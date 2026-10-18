import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import SettingsPage from "./page";

describe("SettingsPage", () => {
  it("titles the station and states its scope, with no save step", () => {
    render(<SettingsPage />);

    expect(screen.getByRole("heading", { name: "Settings", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Applies to this browser.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  });

  it("renders the Appearance section", () => {
    render(<SettingsPage />);
    expect(screen.getByRole("group", { name: "Appearance" })).toBeInTheDocument();
  });

  it("escapes the shell's padding so its header reaches the true edges", () => {
    const { container } = render(<SettingsPage />);
    const root = container.firstElementChild;
    expect(root).toHaveClass("-m-6");

    const header = screen
      .getByRole("heading", { name: "Settings", level: 1 })
      .closest("div.h-\\[70px\\]");
    expect(root?.firstElementChild).toBe(header);
  });
});
