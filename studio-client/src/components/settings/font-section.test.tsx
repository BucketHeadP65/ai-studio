import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { FONT_STORAGE_KEY, FontSection } from "./font-section";

beforeEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.font;
});

afterEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.font;
});

describe("FontSection", () => {
  it("renders one grid cell per face, named and previewing its own variable", () => {
    render(<FontSection />);

    for (const name of [
      "Space Grotesk",
      "Inter",
      "Manrope",
      "IBM Plex Sans",
      "Outfit",
      "DM Sans",
      "Geist",
      "Sora",
      "Plus Jakarta Sans",
      "Work Sans",
      "Figtree",
      "Hanken Grotesk",
      "Instrument Sans",
      "Lexend",
      "Archivo",
    ]) {
      expect(screen.getByRole("radio", { name })).toBeInTheDocument();
    }
    const inter = screen.getByRole("radio", { name: "Inter" }).closest("label");
    expect(inter?.style.fontFamily).toContain("--font-inter");
    expect(inter).toHaveAttribute("title", "The neutral workhorse, drawn for screens.");
  });

  it("checks Space Grotesk by default when nothing is stored", () => {
    render(<FontSection />);
    expect(screen.getByRole("radio", { name: "Space Grotesk" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Inter" })).not.toBeChecked();
  });

  it("checks the face already stored for this browser", () => {
    window.localStorage.setItem(FONT_STORAGE_KEY, "manrope");
    render(<FontSection />);
    expect(screen.getByRole("radio", { name: "Manrope" })).toBeChecked();
  });

  it("stamps the chosen face on the document and persists it", async () => {
    render(<FontSection />);

    await userEvent.click(screen.getByRole("radio", { name: "Inter" }));

    expect(document.documentElement.dataset.font).toBe("inter");
    expect(window.localStorage.getItem(FONT_STORAGE_KEY)).toBe("inter");
  });

  it("picking the default face removes the stamp instead of naming it", async () => {
    window.localStorage.setItem(FONT_STORAGE_KEY, "outfit");
    document.documentElement.dataset.font = "outfit";
    render(<FontSection />);

    await userEvent.click(screen.getByRole("radio", { name: "Space Grotesk" }));

    expect(document.documentElement.dataset.font).toBeUndefined();
    expect(window.localStorage.getItem(FONT_STORAGE_KEY)).toBe("grotesk");
  });
});
