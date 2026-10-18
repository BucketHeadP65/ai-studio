import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { AppearanceSection, THEME_STORAGE_KEY } from "./appearance-section";

beforeEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

afterEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe("AppearanceSection", () => {
  it("renders one radio card per preset with its name and description", () => {
    render(<AppearanceSection />);

    expect(screen.getByRole("group", { name: "Appearance" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Wire" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Factory" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Graphite" })).toBeInTheDocument();
    expect(
      screen.getByText("The light theme, warm neutrals and a vermilion signal."),
    ).toBeInTheDocument();
  });

  it("checks Wire by default when nothing is stored", () => {
    render(<AppearanceSection />);
    expect(screen.getByRole("radio", { name: "Wire" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Factory" })).not.toBeChecked();
  });

  it("checks the preset already stored for this browser", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "graphite");
    render(<AppearanceSection />);
    expect(screen.getByRole("radio", { name: "Graphite" })).toBeChecked();
  });

  it("applies a preset immediately on choice, no save step", async () => {
    const user = userEvent.setup();
    render(<AppearanceSection />);

    await user.click(screen.getByRole("radio", { name: "Factory" }));

    expect(screen.getByRole("radio", { name: "Factory" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Wire" })).not.toBeChecked();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("factory");
    expect(document.documentElement.dataset.theme).toBe("factory");
  });

  it("clears the theme attribute when Wire is chosen back", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(THEME_STORAGE_KEY, "factory");
    document.documentElement.dataset.theme = "factory";
    render(<AppearanceSection />);

    await user.click(screen.getByRole("radio", { name: "Wire" }));

    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("paper");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("keeps the applied preset across a remount", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<AppearanceSection />);

    await user.click(screen.getByRole("radio", { name: "Graphite" }));
    unmount();

    render(<AppearanceSection />);
    expect(screen.getByRole("radio", { name: "Graphite" })).toBeChecked();
  });

  it("marks a card with a focus-visible ring so tabbing through the presets is never invisible", () => {
    render(<AppearanceSection />);
    const card = screen.getByRole("radio", { name: "Wire" }).closest("label");
    expect(card?.className).toContain("has-[:focus-visible]:ring-2");
  });

  it("keeps the presets reachable by keyboard, tabbing onto a radio named for its own preset", async () => {
    const user = userEvent.setup();
    render(<AppearanceSection />);

    await user.tab();
    expect(screen.getByRole("radio", { name: "Wire" })).toHaveFocus();
  });
});
