import { describe, expect, it } from "vitest";

import { formatRelative } from "./format-relative";

const NOW = new Date("2026-05-23T12:00:00Z").getTime();

describe("formatRelative", () => {
  it("returns 'just now' when the delta is under a minute", () => {
    expect(formatRelative("2026-05-23T11:59:30Z", NOW)).toBe("just now");
  });

  it("formats minutes ago when the delta is under an hour", () => {
    expect(formatRelative("2026-05-23T11:55:00Z", NOW)).toBe("5 minutes ago");
  });

  it("formats hours ago when the delta is under a day", () => {
    expect(formatRelative("2026-05-23T10:00:00Z", NOW)).toBe("2 hours ago");
  });

  it("formats days ago when the delta is under a week", () => {
    expect(formatRelative("2026-05-21T12:00:00Z", NOW)).toBe("2 days ago");
  });

  it("falls back to a locale date string past one week", () => {
    const oneMonthAgo = "2026-04-23T12:00:00Z";
    const result = formatRelative(oneMonthAgo, NOW);
    // Locale-specific format, just confirm we got a date string (not a relative).
    expect(result).not.toMatch(/ago|in /);
    expect(result.length).toBeGreaterThan(0);
  });
});
