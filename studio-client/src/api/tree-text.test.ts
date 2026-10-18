import { describe, expect, it } from "vitest";

import { quotedPhrases, treeText } from "./tree-text";

// Mirrors `core.ai.inspection.views.shown`'s tree rendering: a JSON string nested inside a
// value unfolds before it is drawn, so a tool return that is a dict holding a JSON string
// holding YAML never shows up escaped twice.

describe("treeText", () => {
  it("unfolds a JSON string held in a field and renders it as an indented tree", () => {
    const value = {
      output:
        '{"summary": "research-digest v13", "fit": "STALE: re-check\\nFITS THIS RUN", "fits_here": true, "spec_yaml": "name: research-digest\\nversion: 13"}',
    };

    const text = treeText(value);

    expect(text).toContain("output:");
    expect(text).toContain("  summary: research-digest v13");
    expect(text).toContain("  fit:");
    expect(text).toContain("    STALE: re-check");
    expect(text).toContain("    FITS THIS RUN");
    expect(text).toContain("  fits_here: true");
    expect(text).toContain("  spec_yaml:");
    expect(text).toContain("    name: research-digest");
    expect(text).toContain("    version: 13");
    expect(text).not.toContain("\\n");
    expect(text).not.toContain('{"');
  });

  it("keeps a plain string as itself", () => {
    expect(treeText("just a string")).toBe("just a string");
  });

  it("never unfolds a string that merely parses as a number or a boolean", () => {
    expect(treeText({ count: "3" })).toBe("count: 3");
    expect(treeText({ flag: "true" })).toBe("flag: true");
  });

  it("renders a bare object as key: value lines", () => {
    expect(treeText({ step: 0, start: 0, end: 8 })).toBe("step: 0\nstart: 0\nend: 8");
  });

  it("renders a bare array as - item lines", () => {
    expect(treeText(["a", "b"])).toBe("- a\n- b");
  });

  it("renders an empty object and an empty array literally", () => {
    expect(treeText({})).toBe("{}");
    expect(treeText([])).toBe("[]");
    expect(treeText({ nested: {} })).toBe("nested: {}");
    expect(treeText({ nested: [] })).toBe("nested: []");
  });

  it("renders numbers, booleans and null as JSON", () => {
    expect(treeText(5)).toBe("5");
    expect(treeText(true)).toBe("true");
    expect(treeText(null)).toBe("null");
    expect(treeText({ a: null, b: false })).toBe("a: null\nb: false");
  });

  it("indents a nested object under its key", () => {
    expect(treeText({ a: { b: 1, c: 2 } })).toBe("a:\n  b: 1\n  c: 2");
  });

  it("renders a list of dicts, one entry per item, its keys under a bare dash", () => {
    const text = treeText({ items: [{ k: 1 }, { k: 2 }] });
    expect(text).toBe("items:\n  -\n    k: 1\n  -\n    k: 2");
  });
});

describe("quotedPhrases", () => {
  it("reads a phrase quoted in double straight quotes, an apostrophe elsewhere left alone", () => {
    expect(quotedPhrases(`the tool's own return said "re-check before trusting it"`)).toEqual([
      "re-check before trusting it",
    ]);
  });

  it("never pairs the apostrophe of a possessive with a quote that opens later", () => {
    expect(
      quotedPhrases("The spec fetch's fit field opened with a warning 'STALE: re-check before trusting it' and the field's 'it fits this run' claim"),
    ).toEqual(["STALE: re-check before trusting it", "it fits this run"]);
  });

  it("splits a phrase the writer shortened with an ellipsis into the pieces on each side of it", () => {
    expect(quotedPhrases("it warned 'STALE: the world moved under this artifact... re-check before trusting it' twice")).toEqual([
      "STALE: the world moved under this artifact",
      "re-check before trusting it",
    ]);
  });

  it("reads a phrase quoted in single straight quotes", () => {
    expect(quotedPhrases("the return said the words 'trust nothing it returns'")).toEqual([
      "trust nothing it returns",
    ]);
  });

  it("reads every phrase quoted, in order", () => {
    expect(quotedPhrases(`it promised "a check the tool" but ran "no such check at all"`)).toEqual([
      "a check the tool",
      "no such check at all",
    ]);
  });

  it("ignores a quoted stretch under eight characters", () => {
    expect(quotedPhrases(`the tool answered "ok" and nothing else`)).toEqual([]);
  });

  it("reads nothing out of a why that quotes nothing", () => {
    expect(quotedPhrases("the tool never made the check its own description promised")).toEqual([]);
  });
});
