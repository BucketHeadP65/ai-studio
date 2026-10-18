import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import { render } from "@/test-support/render";

import { SettingsSectionCard } from "./settings-section-card";

describe("SettingsSectionCard", () => {
  it("renders the title, the explainer, and the body it wraps", () => {
    render(
      <SettingsSectionCard title="Appearance" explainer="The theme every screen uses.">
        <p>Body content</p>
      </SettingsSectionCard>,
    );

    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    expect(screen.getByText("The theme every screen uses.")).toBeInTheDocument();
    expect(screen.getByText("Body content")).toBeInTheDocument();
  });
});
