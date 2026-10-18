import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FileNoteSummary } from "@/api/projects";

import { markSeen, noteDotTone, readSeenAt } from "./notes-seen";

function summary(overrides: Partial<FileNoteSummary> = {}): FileNoteSummary {
  return {
    path: "readme.md",
    open_notes: 1,
    resolved_notes: 0,
    latest_agent_reply_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("markSeen / readSeenAt", () => {
  it("round-trips the latest agent-reply timestamp for a project and path", () => {
    markSeen("proj-1", "readme.md", "2026-08-11T09:06:00+00:00");

    expect(readSeenAt("proj-1", "readme.md")).toBe("2026-08-11T09:06:00+00:00");
  });

  it("records nothing when there is no agent reply yet", () => {
    markSeen("proj-1", "readme.md", null);

    expect(readSeenAt("proj-1", "readme.md")).toBeNull();
  });

  it("keeps two files, and two projects, apart", () => {
    markSeen("proj-1", "a.md", "2026-08-11T09:00:00+00:00");
    markSeen("proj-1", "b.md", "2026-08-12T09:00:00+00:00");
    markSeen("proj-2", "a.md", "2026-08-13T09:00:00+00:00");

    expect(readSeenAt("proj-1", "a.md")).toBe("2026-08-11T09:00:00+00:00");
    expect(readSeenAt("proj-1", "b.md")).toBe("2026-08-12T09:00:00+00:00");
    expect(readSeenAt("proj-2", "a.md")).toBe("2026-08-13T09:00:00+00:00");
  });

  it("degrades to null, never throws, when localStorage.getItem throws", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    expect(() => readSeenAt("proj-1", "a.md")).not.toThrow();
    expect(readSeenAt("proj-1", "a.md")).toBeNull();
  });

  it("degrades silently, never throws, when localStorage.setItem throws", () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    expect(() => markSeen("proj-1", "a.md", "2026-08-11T09:00:00+00:00")).not.toThrow();
  });
});

describe("noteDotTone", () => {
  it("is none with no summary entry (a non-markdown row, or a file with no notes)", () => {
    expect(noteDotTone("proj-1", undefined)).toBe("none");
  });

  it("is none when both counts are zero", () => {
    expect(noteDotTone("proj-1", summary({ open_notes: 0, resolved_notes: 0 }))).toBe("none");
  });

  it("is seen when there are notes but no agent reply has ever landed", () => {
    expect(noteDotTone("proj-1", summary({ latest_agent_reply_at: null }))).toBe("seen");
  });

  it("is new when the viewer has never recorded seeing this file's agent activity", () => {
    const entry = summary({ latest_agent_reply_at: "2026-08-11T09:06:00+00:00" });

    expect(noteDotTone("proj-1", entry)).toBe("new");
  });

  it("is new when the agent replied again after the viewer's last recorded look", () => {
    markSeen("proj-1", "readme.md", "2026-08-11T09:00:00+00:00");
    const entry = summary({ latest_agent_reply_at: "2026-08-11T09:06:00+00:00" });

    expect(noteDotTone("proj-1", entry)).toBe("new");
  });

  it("is seen once the viewer's recorded look catches up to the latest agent reply", () => {
    markSeen("proj-1", "readme.md", "2026-08-11T09:06:00+00:00");
    const entry = summary({ latest_agent_reply_at: "2026-08-11T09:06:00+00:00" });

    expect(noteDotTone("proj-1", entry)).toBe("seen");
  });
});
