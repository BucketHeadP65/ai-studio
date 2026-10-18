import { describe, expect, it } from "vitest";

import type { FileEntry } from "@/api/projects";

import { pathFromWikilinkHref, resolveWikilinkTarget, wikilinkHref } from "./wikilinks";

const ENTRIES: FileEntry[] = [
  { path: "notes", kind: "dir", size: 0 },
  { path: "notes/Home.md", kind: "file", size: 10 },
  { path: "readme.md", kind: "file", size: 10 },
  { path: "archive/readme.md", kind: "file", size: 10 },
  { path: "image.png", kind: "file", size: 10 },
];

describe("resolveWikilinkTarget", () => {
  it("prefers an exact relative path, with or without the extension", () => {
    expect(resolveWikilinkTarget(ENTRIES, "notes/Home.md")).toBe("notes/Home.md");
    expect(resolveWikilinkTarget(ENTRIES, "notes/Home")).toBe("notes/Home.md");
  });

  it("prefers the exact path over a same-named file elsewhere", () => {
    expect(resolveWikilinkTarget(ENTRIES, "readme.md")).toBe("readme.md");
  });

  it("falls back to a unique basename anywhere in the project", () => {
    expect(resolveWikilinkTarget(ENTRIES, "Home")).toBe("notes/Home.md");
  });

  it("refuses an ambiguous basename", () => {
    expect(resolveWikilinkTarget(ENTRIES, "readme")).toBe("readme.md");
    expect(
      resolveWikilinkTarget(
        [
          { path: "a/dup.md", kind: "file", size: 1 },
          { path: "b/dup.md", kind: "file", size: 1 },
        ],
        "dup",
      ),
    ).toBeNull();
  });

  it("ignores directories and unknown or empty targets", () => {
    expect(resolveWikilinkTarget(ENTRIES, "notes")).toBeNull();
    expect(resolveWikilinkTarget(ENTRIES, "nothing here")).toBeNull();
    expect(resolveWikilinkTarget(ENTRIES, "   ")).toBeNull();
  });

  it("resolves a non-markdown file by its exact name", () => {
    expect(resolveWikilinkTarget(ENTRIES, "image.png")).toBe("image.png");
  });
});

describe("wikilinkHref", () => {
  it("round-trips a path, spaces and slashes included", () => {
    const href = wikilinkHref("notes/my page.md");

    expect(href).not.toBeNull();
    expect(pathFromWikilinkHref(href)).toBe("notes/my page.md");
  });

  it("keeps an unresolved target unresolved", () => {
    expect(wikilinkHref(null)).toBeNull();
  });

  it("reads no path out of an ordinary href", () => {
    expect(pathFromWikilinkHref("https://example.com")).toBeNull();
    expect(pathFromWikilinkHref(null)).toBeNull();
  });
});
