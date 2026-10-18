import { afterEach, describe, expect, it } from "vitest";

import { isDarkAppTheme, stampProseTheme } from "./use-app-dark";

afterEach(() => {
  delete document.documentElement.dataset.theme;
});

describe("isDarkAppTheme", () => {
  it("is dark for the two dark html[data-theme] values", () => {
    expect(isDarkAppTheme("factory")).toBe(true);
    expect(isDarkAppTheme("graphite")).toBe(true);
  });

  it("is light for paper (no attribute) and any unrecognized value", () => {
    expect(isDarkAppTheme(undefined)).toBe(false);
    expect(isDarkAppTheme("paper")).toBe(false);
    expect(isDarkAppTheme("light")).toBe(false);
  });
});

describe("stampProseTheme", () => {
  it("stamps dark and clears it again", () => {
    const element = document.createElement("div");

    stampProseTheme(element, true);
    expect(element.dataset.theme).toBe("dark");

    stampProseTheme(element, false);
    expect(element.hasAttribute("data-theme")).toBe(false);
  });

  it("ignores a missing element", () => {
    expect(() => stampProseTheme(null, true)).not.toThrow();
  });
});
