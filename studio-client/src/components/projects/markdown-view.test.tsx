import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MarkdownView, mapSelectionToSourceSpan, selectionEndPosition } from "./markdown-view";

afterEach(cleanup);

describe("<MarkdownView />", () => {
  it("stamps data-sourcepos on the top-level block with the exact character offsets", () => {
    const source = "# Title\n\nA paragraph.\n";
    const { container } = render(<MarkdownView source={source} />);

    const heading = container.querySelector("h1");
    const headingStart = source.indexOf("# Title");
    const headingEnd = headingStart + "# Title".length;
    expect(heading).toHaveAttribute("data-sourcepos", `${headingStart}-${headingEnd}`);

    const paragraph = container.querySelector("p");
    const paraStart = source.indexOf("A paragraph.");
    const paraEnd = paraStart + "A paragraph.".length;
    expect(paragraph).toHaveAttribute("data-sourcepos", `${paraStart}-${paraEnd}`);
  });

  it("stamps data-sourcepos correctly on nested inline nodes inside a blockquote", () => {
    const source = "> A *quoted* line with **bold** text and `code`.\n";
    const { container } = render(<MarkdownView source={source} />);

    const blockquote = container.querySelector("blockquote");
    const blockquoteStart = source.indexOf(">");
    expect(blockquote).toHaveAttribute(
      "data-sourcepos",
      `${blockquoteStart}-${source.trimEnd().length}`,
    );

    const emphasis = screen.getByText("quoted");
    const emStart = source.indexOf("*quoted*");
    const emEnd = emStart + "*quoted*".length;
    expect(emphasis.tagName).toBe("EM");
    expect(emphasis).toHaveAttribute("data-sourcepos", `${emStart}-${emEnd}`);

    const strong = screen.getByText("bold");
    const strongStart = source.indexOf("**bold**");
    const strongEnd = strongStart + "**bold**".length;
    expect(strong.tagName).toBe("STRONG");
    expect(strong).toHaveAttribute("data-sourcepos", `${strongStart}-${strongEnd}`);

    const code = screen.getByText("code");
    const codeStart = source.indexOf("`code`");
    const codeEnd = codeStart + "`code`".length;
    expect(code.tagName).toBe("CODE");
    expect(code).toHaveAttribute("data-sourcepos", `${codeStart}-${codeEnd}`);
  });

  it("stamps data-sourcepos on each list item independently", () => {
    const source = "- first item\n- second item\n";
    const { container } = render(<MarkdownView source={source} />);

    const items = container.querySelectorAll("li");
    expect(items).toHaveLength(2);

    const firstStart = source.indexOf("- first item");
    const firstEnd = firstStart + "- first item".length;
    expect(items[0]).toHaveAttribute("data-sourcepos", `${firstStart}-${firstEnd}`);

    const secondStart = source.indexOf("- second item");
    const secondEnd = secondStart + "- second item".length;
    expect(items[1]).toHaveAttribute("data-sourcepos", `${secondStart}-${secondEnd}`);
  });

  it("resolves [[target]] wikilinks to anchors via resolveWikilink", () => {
    const source = "See [[Home]] for more.";
    const resolveWikilink = vi.fn((target: string) => (target === "Home" ? "/wiki/home" : null));

    render(<MarkdownView source={source} resolveWikilink={resolveWikilink} />);

    const link = screen.getByRole("link", { name: "Home" });
    expect(link).toHaveAttribute("href", "/wiki/home");
    expect(resolveWikilink).toHaveBeenCalledWith("Home");
  });

  it("resolves [[target|label]] wikilinks using the label as display text", () => {
    const source = "See [[Other Page|the other page]] for more.";
    const resolveWikilink = vi.fn((target: string) =>
      target === "Other Page" ? "/wiki/other-page" : null,
    );

    render(<MarkdownView source={source} resolveWikilink={resolveWikilink} />);

    const link = screen.getByRole("link", { name: "the other page" });
    expect(link).toHaveAttribute("href", "/wiki/other-page");
  });

  it("renders an unresolved wikilink as a dead-link span, not an anchor", () => {
    const source = "See [[Missing]] for more.";
    const resolveWikilink = vi.fn(() => null);

    const { container } = render(
      <MarkdownView source={source} resolveWikilink={resolveWikilink} />,
    );

    expect(screen.queryByRole("link", { name: "Missing" })).not.toBeInTheDocument();
    const deadSpan = screen.getByText("Missing");
    expect(deadSpan.tagName).toBe("SPAN");
    expect(container.querySelector("a")).not.toBeInTheDocument();
  });

  it("treats a wikilink as dead when no resolveWikilink prop is given", () => {
    const source = "See [[Home]] for more.";

    render(<MarkdownView source={source} />);

    expect(screen.queryByRole("link", { name: "Home" })).not.toBeInTheDocument();
    expect(screen.getByText("Home").tagName).toBe("SPAN");
  });

  it("carries data-sourcepos on wikilink nodes too", () => {
    const source = "See [[Home]] for more.";
    const start = source.indexOf("[[Home]]");
    const end = start + "[[Home]]".length;

    render(<MarkdownView source={source} resolveWikilink={() => "/wiki/home"} />);

    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute(
      "data-sourcepos",
      `${start}-${end}`,
    );
  });

  it("computes correct raw-source data-sourcepos for a wikilink after an HTML entity in the same text run", () => {
    const source = "Q&amp;A then [[Home]].";
    const start = source.indexOf("[[Home]]");
    const end = start + "[[Home]]".length;

    render(<MarkdownView source={source} resolveWikilink={() => "/wiki/home"} />);

    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute(
      "data-sourcepos",
      `${start}-${end}`,
    );
  });

  it("omits data-sourcepos for a wikilink whose raw text can't be matched verbatim (an internal escape)", () => {
    const source = "See [[Foo\\_Bar]] here.";

    render(<MarkdownView source={source} resolveWikilink={() => "/wiki/foo-bar"} />);

    const link = screen.getByRole("link", { name: "Foo_Bar" });
    expect(link).not.toHaveAttribute("data-sourcepos");
  });

  it("scopes the class on the root element and merges a caller-provided className", () => {
    const { container } = render(<MarkdownView source="hello" className="extra" />);
    const root = container.firstElementChild;
    expect(root).toHaveClass("projects-prose");
    expect(root).toHaveClass("extra");
  });

  it("wraps a GFM table in its own scrollable container, without leaking react-markdown's node prop", () => {
    const source = "| a | b |\n| --- | --- |\n| 1 | 2 |\n";
    const { container } = render(<MarkdownView source={source} />);

    const wrap = container.querySelector(".table-wrap");
    expect(wrap).toBeInTheDocument();
    const table = wrap?.querySelector("table");
    expect(table).toBeInTheDocument();
    expect(table).not.toHaveAttribute("node");
  });

  it("fires onSelectSpan with the mapped source offsets and the selection's end position", () => {
    const source = "A paragraph of plain text.";
    const onSelectSpan = vi.fn();
    const { container } = render(<MarkdownView source={source} onSelectSpan={onSelectSpan} />);

    const paragraph = container.querySelector("p");
    if (!paragraph) throw new Error("expected a rendered paragraph");
    const textNode = paragraph.firstChild;
    if (!textNode) throw new Error("expected the paragraph to have text content");

    const range = document.createRange();
    range.setStart(textNode, 2);
    range.setEnd(textNode, 11);
    // jsdom does no layout, so a real Range's getClientRects() is always empty --
    // stubbed here the same way the app measures a real selection's end.
    range.getClientRects = () =>
      [
        { top: 10, left: 20, bottom: 30, right: 40, width: 20, height: 20 },
      ] as unknown as DOMRectList;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);

    paragraph.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));

    const start = source.indexOf("paragraph");
    const end = start + "paragraph".length;
    expect(onSelectSpan).toHaveBeenCalledWith(start, end, { top: 30, left: 40 });
  });

  it("does not fire onSelectSpan for a collapsed selection", () => {
    const source = "A paragraph of plain text.";
    const onSelectSpan = vi.fn();
    const { container } = render(<MarkdownView source={source} onSelectSpan={onSelectSpan} />);

    const paragraph = container.querySelector("p");
    if (!paragraph) throw new Error("expected a rendered paragraph");
    const textNode = paragraph.firstChild;
    if (!textNode) throw new Error("expected the paragraph to have text content");

    const range = document.createRange();
    range.setStart(textNode, 2);
    range.setEnd(textNode, 2);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);

    paragraph.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));

    expect(onSelectSpan).not.toHaveBeenCalled();
  });
});

describe("mapSelectionToSourceSpan", () => {
  function textNodeIn(container: HTMLElement, text: string): Text {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      if (node.textContent === text) return node as Text;
      node = walker.nextNode();
    }
    throw new Error(`no text node with content ${JSON.stringify(text)}`);
  }

  it("returns null for a collapsed selection", () => {
    const container = document.createElement("div");
    container.innerHTML = '<p data-sourcepos="0-11">hello world</p>';
    const node = textNodeIn(container, "hello world");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: true,
        toString: () => "",
        anchorNode: node,
        anchorOffset: 0,
        focusNode: node,
        focusOffset: 0,
      },
      "hello world",
    );

    expect(result).toBeNull();
  });

  it("returns null when the selected string is only whitespace", () => {
    const container = document.createElement("div");
    container.innerHTML = '<p data-sourcepos="0-11">hello world</p>';
    const node = textNodeIn(container, "hello world");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "   ",
        anchorNode: node,
        anchorOffset: 0,
        focusNode: node,
        focusOffset: 0,
      },
      "hello world",
    );

    expect(result).toBeNull();
  });

  it("returns null when the selection reaches outside the container", () => {
    const container = document.createElement("div");
    container.innerHTML = '<p data-sourcepos="0-11">hello world</p>';
    const inside = textNodeIn(container, "hello world");
    const outside = document.createTextNode("elsewhere");
    document.body.append(outside);

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "hello",
        anchorNode: inside,
        anchorOffset: 0,
        focusNode: outside,
        focusOffset: 0,
      },
      "hello world",
    );

    expect(result).toBeNull();
    outside.remove();
  });

  it("returns null when an endpoint is an element, whose offset counts children, not characters", () => {
    const source = "A *em* tail.";
    const container = document.createElement("div");
    container.innerHTML = '<p data-sourcepos="0-12">A <em data-sourcepos="2-6">em</em> tail.</p>';
    const anchor = textNodeIn(container, "A ");
    const focus = container.querySelector("em");
    if (!focus) throw new Error("no em element");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        // The rendered text differs from the raw source (no asterisks), so the exact fast
        // path fails and the endpoint fallback must judge the element endpoint itself.
        toString: () => "A em",
        anchorNode: anchor,
        anchorOffset: 0,
        focusNode: focus,
        focusOffset: 1,
      },
      source,
    );

    expect(result).toBeNull();
  });

  it("maps a selection inside one block to the block's source offsets", () => {
    const source = "before\n\nhello world\n\nafter";
    const container = document.createElement("div");
    const blockStart = source.indexOf("hello world");
    const blockEnd = blockStart + "hello world".length;
    container.innerHTML = `<p data-sourcepos="${blockStart}-${blockEnd}">hello world</p>`;
    const node = textNodeIn(container, "hello world");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "world",
        anchorNode: node,
        anchorOffset: 0,
        focusNode: node,
        focusOffset: 0,
      },
      source,
    );

    const expectedStart = source.indexOf("world");
    expect(result).toEqual({
      sourceStart: expectedStart,
      sourceEnd: expectedStart + "world".length,
    });
  });

  it("walks up to the nearest common ancestor block when anchor and focus differ", () => {
    const source = "A *em* and **strong** span.";
    const blockStart = 0;
    const blockEnd = source.length;
    const container = document.createElement("div");
    const emStart = source.indexOf("em");
    const strongStart = source.indexOf("strong");
    container.innerHTML =
      `<p data-sourcepos="${blockStart}-${blockEnd}">A ` +
      `<em data-sourcepos="${emStart - 1}-${emStart + 3}">em</em> and ` +
      `<strong data-sourcepos="${strongStart - 2}-${strongStart + 8}">strong</strong> span.</p>`;
    const anchor = textNodeIn(container, "em");
    const focus = textNodeIn(container, "strong");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "em* and **strong",
        anchorNode: anchor,
        anchorOffset: 0,
        focusNode: focus,
        focusOffset: 0,
      },
      source,
    );

    expect(result).not.toBeNull();
    expect(source.slice(result?.sourceStart, result?.sourceEnd)).toBe("em* and **strong");
  });

  it("maps a selection across formatting via its endpoints when the rendered text differs from source", () => {
    const source = "A *em* and **strong** span.";
    const container = document.createElement("div");
    container.innerHTML =
      '<p data-sourcepos="0-27">A <em data-sourcepos="2-6">em</em> and ' +
      '<strong data-sourcepos="11-21">strong</strong> span.</p>';
    const anchor = textNodeIn(container, "em");
    const focus = textNodeIn(container, "strong");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "em and strong",
        anchorNode: anchor,
        anchorOffset: 0,
        focusNode: focus,
        focusOffset: 6,
      },
      source,
    );

    expect(result).not.toBeNull();
    expect(source.slice(result?.sourceStart, result?.sourceEnd)).toBe("em* and **strong");
  });

  it("returns null when the selected text cannot be located inside the block's source slice", () => {
    const source = "hello world";
    const container = document.createElement("div");
    container.innerHTML = '<p data-sourcepos="0-11">hello world</p>';
    const node = textNodeIn(container, "hello world");

    const result = mapSelectionToSourceSpan(
      container,
      {
        isCollapsed: false,
        toString: () => "not present",
        anchorNode: node,
        anchorOffset: 0,
        focusNode: node,
        focusOffset: 0,
      },
      source,
    );

    expect(result).toBeNull();
  });
});

describe("selectionEndPosition", () => {
  afterEach(() => window.getSelection()?.removeAllRanges());

  function fakeSelection(rects: Array<Pick<DOMRect, "bottom" | "right">>): Selection {
    const range = {
      getClientRects: () => rects as unknown as DOMRectList,
    } as unknown as Range;
    return {
      rangeCount: 1,
      getRangeAt: () => range,
    } as unknown as Selection;
  }

  it("returns null when the selection has no range", () => {
    const selection = { rangeCount: 0, getRangeAt: () => null } as unknown as Selection;

    expect(selectionEndPosition(selection)).toBeNull();
  });

  it("returns null when the range paints no client rects", () => {
    expect(selectionEndPosition(fakeSelection([]))).toBeNull();
  });

  it("takes the bottom-right corner of the LAST rect, for a selection spanning multiple lines", () => {
    const selection = fakeSelection([
      { bottom: 20, right: 200 },
      { bottom: 40, right: 60 },
    ]);

    expect(selectionEndPosition(selection)).toEqual({ top: 40, left: 60 });
  });
});
