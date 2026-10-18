import { describe, expect, it } from "vitest";

import {
  blocksForSourceSpan,
  elementCoveringOffset,
  hasLayout,
  lineStartOffset,
  narrowestCoveringSpan,
  rangeForSourceSpan,
  sourceOffsetOfDomPoint,
} from "./source-positions";

function container(html: string): HTMLElement {
  const element = document.createElement("div");
  element.innerHTML = html;
  return element;
}

describe("lineStartOffset", () => {
  const source = "alpha\nbeta\n\ngamma";

  it("starts line 1 at the beginning", () => {
    expect(lineStartOffset(source, 1)).toBe(0);
    expect(lineStartOffset(source, 0)).toBe(0);
  });

  it("counts newlines for later lines, blank lines included", () => {
    expect(lineStartOffset(source, 2)).toBe(6);
    expect(lineStartOffset(source, 3)).toBe(11);
    expect(lineStartOffset(source, 4)).toBe(12);
  });

  it("clamps a line past the end of the source", () => {
    expect(lineStartOffset(source, 99)).toBe(source.length);
  });
});

describe("elementCoveringOffset", () => {
  it("picks the narrowest element covering the offset", () => {
    const root = container(
      '<p data-sourcepos="0-20">outer<a data-sourcepos="6-12" id="inner">inner</a></p>',
    );

    expect(elementCoveringOffset(root, 8)?.id).toBe("inner");
    expect(elementCoveringOffset(root, 2)?.tagName).toBe("P");
  });

  it("falls forward to the first element starting after an uncovered offset", () => {
    const root = container(
      '<p data-sourcepos="0-5" id="first">a</p><p data-sourcepos="20-25" id="second">b</p>',
    );

    expect(elementCoveringOffset(root, 10)?.id).toBe("second");
  });

  it("is null when nothing covers or follows the offset", () => {
    const root = container('<p data-sourcepos="0-5">a</p>');

    expect(elementCoveringOffset(root, 40)).toBeNull();
  });

  it("ignores a malformed range", () => {
    const root = container('<p data-sourcepos="not-a-range">a</p>');

    expect(elementCoveringOffset(root, 0)).toBeNull();
  });
});

describe("hasLayout", () => {
  it("is false under jsdom, where every box measures zero", () => {
    expect(hasLayout(container("<p>a</p>"))).toBe(false);
  });
});

describe("blocksForSourceSpan", () => {
  it("returns the single block covering both endpoints", () => {
    const root = container('<p data-sourcepos="0-20" id="only">one two three four</p>');

    const blocks = blocksForSourceSpan(root, 4, 7);

    expect(blocks.map((element) => element.id)).toEqual(["only"]);
  });

  it("returns both endpoint blocks when the span crosses a boundary", () => {
    const root = container(
      '<p data-sourcepos="0-5" id="first">alpha</p><p data-sourcepos="5-11" id="second">beta</p>',
    );

    const blocks = blocksForSourceSpan(root, 2, 8);

    expect(blocks.map((element) => element.id)).toEqual(["first", "second"]);
  });

  it("is empty for an inverted or empty span", () => {
    const root = container('<p data-sourcepos="0-20">text</p>');

    expect(blocksForSourceSpan(root, 5, 5)).toEqual([]);
    expect(blocksForSourceSpan(root, 5, 2)).toEqual([]);
  });
});

describe("rangeForSourceSpan", () => {
  it("maps a plain span inside one text node to the exact characters", () => {
    const root = container('<p data-sourcepos="0-18">one two three four</p>');
    const source = "one two three four";

    const range = rangeForSourceSpan(root, 4, 7, source);

    expect(range).not.toBeNull();
    expect(range?.toString()).toBe("two");
  });

  it("maps a span whose text sits inside a nested element (e.g. emphasis), skipping its markers", () => {
    // Source: "one **two** three" -- "two" itself is raw indices 6-9; the em's own
    // data-sourcepos (4-11) covers the markup INCLUDING its `**` markers, exactly as a
    // real remark-stamped tree would report it.
    const root = container(
      '<p data-sourcepos="0-17">one <em data-sourcepos="4-11">two</em> three</p>',
    );
    const source = "one **two** three";

    const range = rangeForSourceSpan(root, 6, 9, source);

    expect(range).not.toBeNull();
    expect(range?.toString()).toBe("two");
  });

  it("is null for an inverted or empty span", () => {
    const root = container('<p data-sourcepos="0-18">one two three four</p>');

    expect(rangeForSourceSpan(root, 7, 4, "one two three four")).toBeNull();
    expect(rangeForSourceSpan(root, 4, 4, "one two three four")).toBeNull();
  });

  it("is null when the span falls outside any covering block", () => {
    const root = container('<p data-sourcepos="0-5">alpha</p>');

    expect(rangeForSourceSpan(root, 50, 55, "alpha")).toBeNull();
  });

  it("is null when the offset lands in syntax the renderer dropped, never a wrong range", () => {
    // The block's rendered text ("bold") never appears verbatim in the raw source slice
    // ("**bold**") at the searched position beyond the markers themselves, so a span that
    // asks for characters INSIDE the `**` markers has no real text node to point at there.
    const root = container('<p data-sourcepos="0-8"><strong>bold</strong></p>');

    expect(rangeForSourceSpan(root, 0, 2, "**bold**")).toBeNull();
  });
});

describe("sourceOffsetOfDomPoint", () => {
  it("maps a point inside a block's own text node back to its source offset", () => {
    const root = container('<p data-sourcepos="0-5">Hello</p>');
    const textNode = root.querySelector("p")?.firstChild;
    if (!textNode) throw new Error("expected a text node");

    expect(sourceOffsetOfDomPoint(root, textNode, 0, "Hello")).toBe(0);
    expect(sourceOffsetOfDomPoint(root, textNode, 3, "Hello")).toBe(3);
  });

  it("walks up to the nearest data-sourcepos ancestor from a nested text node", () => {
    const root = container('<p data-sourcepos="0-13">one <em>two</em> three</p>');
    const emText = root.querySelector("em")?.firstChild;
    if (!emText) throw new Error("expected a text node");

    expect(sourceOffsetOfDomPoint(root, emText, 1, "one two three")).toBe(5);
  });

  it("is null for an element node, never a text node's offset", () => {
    const root = container('<p data-sourcepos="0-5">Hello</p>');
    const paragraph = root.querySelector("p");
    if (!paragraph) throw new Error("expected a paragraph");

    expect(sourceOffsetOfDomPoint(root, paragraph, 0, "Hello")).toBeNull();
  });

  it("is null when the node has no covering data-sourcepos ancestor", () => {
    const root = container("<p>Hello</p>");
    const textNode = root.querySelector("p")?.firstChild;
    if (!textNode) throw new Error("expected a text node");

    expect(sourceOffsetOfDomPoint(root, textNode, 0, "Hello")).toBeNull();
  });

  it("is null when the node's text does not occur in its block's source slice", () => {
    const root = container('<p data-sourcepos="0-5">Hello</p>');
    const textNode = root.querySelector("p")?.firstChild;
    if (!textNode) throw new Error("expected a text node");

    expect(sourceOffsetOfDomPoint(root, textNode, 0, "xxxxx")).toBeNull();
  });
});

describe("narrowestCoveringSpan", () => {
  it("picks the span whose range covers the offset among two adjacent, non-overlapping spans", () => {
    const spans = [
      { id: "a", start: 0, end: 2 },
      { id: "b", start: 2, end: 5 },
    ];

    expect(narrowestCoveringSpan(spans, 1)?.id).toBe("a");
    expect(narrowestCoveringSpan(spans, 3)?.id).toBe("b");
  });

  it("resolves an adjacent boundary offset to the span that starts there, not the one that ends there", () => {
    const spans = [
      { id: "a", start: 0, end: 2 },
      { id: "b", start: 2, end: 5 },
    ];

    expect(narrowestCoveringSpan(spans, 2)?.id).toBe("b");
  });

  it("picks the narrower of two overlapping spans", () => {
    const spans = [
      { id: "outer", start: 0, end: 10 },
      { id: "inner", start: 3, end: 5 },
    ];

    expect(narrowestCoveringSpan(spans, 4)?.id).toBe("inner");
  });

  it("is null when no span covers the offset", () => {
    const spans = [{ id: "a", start: 0, end: 2 }];

    expect(narrowestCoveringSpan(spans, 5)).toBeNull();
  });
});
