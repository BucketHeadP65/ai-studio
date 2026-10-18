import type { Properties as HastProperties } from "hast";
import type {
  Link as MdastLink,
  Node as MdastNode,
  Parent as MdastParent,
  Root as MdastRoot,
  Text as MdastText,
} from "mdast";
import type { JSX } from "react";
import type { PluggableList } from "unified";

import clsx from "clsx";
import { useMemo, useRef } from "react";
import ReactMarkdown, { type ExtraProps } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import "./prose.css";

const SOURCE_POS_ATTRIBUTE = "data-sourcepos";
const WIKILINK_TARGET_ATTRIBUTE = "data-wikilink-target";
const WIKILINK_PATTERN = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

/** A source-character span, `[sourceStart, sourceEnd)`, into a `MarkdownView`'s `source` string. */
export interface SourceSpan {
  sourceStart: number;
  sourceEnd: number;
}

/** A viewport point, for a `position: fixed` element to sit at. */
export interface SelectionPosition {
  top: number;
  left: number;
}

/** The slice of `Selection` the mapping needs — narrow enough to fake in a test. */
export interface SelectionLike {
  isCollapsed: boolean;
  toString: () => string;
  anchorNode: Node | null;
  focusNode: Node | null;
  anchorOffset: number;
  focusOffset: number;
}

function isMdastParent(node: MdastNode): node is MdastParent {
  return "children" in node && Array.isArray(node.children);
}

function withSourcePos(
  properties: HastProperties | undefined,
  start: number,
  end: number,
): HastProperties {
  return { ...properties, [SOURCE_POS_ATTRIBUTE]: `${start}-${end}` };
}

function stampSourcePos(node: MdastNode): void {
  const position = node.position;
  if (
    position &&
    typeof position.start.offset === "number" &&
    typeof position.end.offset === "number"
  ) {
    node.data = {
      ...node.data,
      hProperties: withSourcePos(
        node.data?.hProperties,
        position.start.offset,
        position.end.offset,
      ),
    };
  }
  if (isMdastParent(node)) {
    for (const child of node.children) stampSourcePos(child);
  }
}

/** Remark plugin: copies every node's `position` offsets onto `data.hProperties["data-sourcepos"]`. */
function remarkSourcePos() {
  return (tree: MdastRoot) => {
    stampSourcePos(tree);
  };
}

/**
 * A matched wikilink's raw-source position, found by searching for its exact matched
 * text (as it reads once decoded) inside the text node's own raw source slice — never
 * by adding a decoded-string index onto the node's raw offset, which drifts as soon as
 * the same text run contains an entity or backslash escape earlier in its raw form.
 * Returns `undefined` when the matched text can't be found verbatim in the raw slice
 * (e.g. a decoded escape sits inside the match itself): an untrustworthy position is
 * omitted, never reported wrong.
 */
function locateRawMatch(
  rawSlice: string | null,
  nodeStart: number | undefined,
  matchText: string,
  searchFrom: number,
): { position: MdastText["position"]; nextSearchFrom: number } {
  if (rawSlice === null || nodeStart === undefined) {
    return { position: undefined, nextSearchFrom: searchFrom };
  }
  const rawIndex = rawSlice.indexOf(matchText, searchFrom);
  if (rawIndex === -1) {
    return { position: undefined, nextSearchFrom: searchFrom };
  }
  const start = nodeStart + rawIndex;
  const end = start + matchText.length;
  return {
    position: {
      start: { line: 1, column: 1, offset: start },
      end: { line: 1, column: 1, offset: end },
    },
    nextSearchFrom: rawIndex + matchText.length,
  };
}

function splitWikilinkText(node: MdastText, source: string): Array<MdastText | MdastLink> {
  const value = node.value;
  const matches = [...value.matchAll(WIKILINK_PATTERN)];
  if (matches.length === 0) return [node];

  const nodeStart = node.position?.start.offset;
  const nodeEnd = node.position?.end.offset;
  const rawSlice =
    nodeStart !== undefined && nodeEnd !== undefined ? source.slice(nodeStart, nodeEnd) : null;

  const parts: Array<MdastText | MdastLink> = [];
  let cursor = 0;
  let rawSearchFrom = 0;
  for (const match of matches) {
    const index = match.index;
    if (index === undefined) continue;
    if (index > cursor) {
      parts.push({ type: "text", value: value.slice(cursor, index) });
    }
    const target = match[1].trim();
    const label = (match[2] ?? match[1]).trim();
    const matchText = match[0];
    const located = locateRawMatch(rawSlice, nodeStart, matchText, rawSearchFrom);
    rawSearchFrom = located.nextSearchFrom;
    parts.push({
      type: "link",
      url: `wikilink:${encodeURIComponent(target)}`,
      children: [{ type: "text", value: label }],
      data: { hProperties: { [WIKILINK_TARGET_ATTRIBUTE]: target } },
      position: located.position,
    });
    cursor = index + matchText.length;
  }
  if (cursor < value.length) {
    parts.push({ type: "text", value: value.slice(cursor) });
  }
  return parts;
}

function splitWikilinksIn(node: MdastNode, source: string): void {
  if (!isMdastParent(node)) return;
  const nextChildren: MdastParent["children"] = [];
  for (const child of node.children) {
    if (child.type === "text") {
      nextChildren.push(...splitWikilinkText(child, source));
      continue;
    }
    splitWikilinksIn(child, source);
    nextChildren.push(child);
  }
  node.children = nextChildren;
}

/**
 * Remark plugin: rewrites literal `[[target]]` / `[[target|label]]` text into standard
 * mdast `link` nodes carrying a `data-wikilink-target` marker, resolved to a real href
 * (or a dead-link style) at render time by `MarkdownView`'s anchor renderer. Takes the
 * raw `source` so it can verify each match's position against the raw text rather than
 * trusting decoded-string offsets.
 */
function remarkWikilinks(source: string) {
  return (tree: MdastRoot) => {
    splitWikilinksIn(tree, source);
  };
}

const REHYPE_PLUGINS = [rehypeHighlight];

function readStringProperty(properties: HastProperties, name: string): string | null {
  const value = properties[name];
  return typeof value === "string" ? value : null;
}

type AnchorProps = JSX.IntrinsicElements["a"] & ExtraProps;

function makeAnchorRenderer(resolveWikilink: ((target: string) => string | null) | undefined) {
  return function WikilinkAwareAnchor({ node, href, children, ...rest }: AnchorProps): JSX.Element {
    const target = node ? readStringProperty(node.properties, WIKILINK_TARGET_ATTRIBUTE) : null;
    if (target === null) {
      return (
        <a href={href} {...rest}>
          {children}
        </a>
      );
    }

    const sourcePos = node ? readStringProperty(node.properties, SOURCE_POS_ATTRIBUTE) : null;
    const resolved = resolveWikilink?.(target) ?? null;
    if (resolved === null) {
      return (
        <span className="wikilink-dead" data-sourcepos={sourcePos ?? undefined}>
          {children}
        </span>
      );
    }
    return (
      <a className="wikilink" href={resolved} data-sourcepos={sourcePos ?? undefined}>
        {children}
      </a>
    );
  };
}

function TableWithScroll({
  node: _node,
  children,
  ...rest
}: JSX.IntrinsicElements["table"] & ExtraProps): JSX.Element {
  return (
    <div className="table-wrap">
      <table {...rest}>{children}</table>
    </div>
  );
}

function nearestSourcePosElement(node: Node, container: HTMLElement): Element | null {
  let current: Element | null = node instanceof Element ? node : node.parentElement;
  while (current && container.contains(current)) {
    if (current.hasAttribute(SOURCE_POS_ATTRIBUTE)) return current;
    current = current.parentElement;
  }
  return null;
}

function commonSourcePosElement(a: Element, b: Element, container: HTMLElement): Element | null {
  if (a === b) return a;
  let current: Element | null = a;
  while (current && container.contains(current)) {
    if (current.hasAttribute(SOURCE_POS_ATTRIBUTE) && current.contains(b)) return current;
    current = current.parentElement;
  }
  return null;
}

function parseSourcePos(value: string | null): SourceSpan | null {
  if (!value) return null;
  const match = /^(\d+)-(\d+)$/.exec(value);
  if (!match) return null;
  return { sourceStart: Number(match[1]), sourceEnd: Number(match[2]) };
}

/**
 * Maps a DOM selection inside a rendered `MarkdownView` back to a character span in
 * its raw `source`. Walks up from the selection's anchor/focus to the nearest common
 * `data-sourcepos` ancestor, then locates the selected string inside that block's
 * source slice. Returns `null` for a collapsed, empty (after trim), out-of-container,
 * or unmappable selection.
 */
export function mapSelectionToSourceSpan(
  container: HTMLElement,
  selection: SelectionLike,
  source: string,
): SourceSpan | null {
  if (selection.isCollapsed) return null;
  const text = selection.toString();
  if (text.trim().length === 0) return null;
  if (!selection.anchorNode || !container.contains(selection.anchorNode)) return null;
  if (!selection.focusNode || !container.contains(selection.focusNode)) return null;

  const anchorBlock = nearestSourcePosElement(selection.anchorNode, container);
  const focusBlock = nearestSourcePosElement(selection.focusNode, container);
  if (!anchorBlock || !focusBlock) return null;

  const block = commonSourcePosElement(anchorBlock, focusBlock, container);
  if (block) {
    const span = parseSourcePos(block.getAttribute(SOURCE_POS_ATTRIBUTE));
    if (span) {
      const blockSlice = source.slice(span.sourceStart, span.sourceEnd);
      const index = blockSlice.indexOf(text);
      if (index !== -1) {
        return {
          sourceStart: span.sourceStart + index,
          sourceEnd: span.sourceStart + index + text.length,
        };
      }
    }
  }

  // The exact match above fails whenever the rendered text differs from the raw source (a
  // selection crossing `**bold**`, a link, a wikilink alias) and whenever the selection spans
  // blocks. Each ENDPOINT still maps reliably: a DOM text node never crosses markdown syntax,
  // so its text occurs verbatim in its own block's source slice.
  const start = sourceOffsetOfPoint(
    selection.anchorNode,
    selection.anchorOffset,
    container,
    source,
  );
  const end = sourceOffsetOfPoint(selection.focusNode, selection.focusOffset, container, source);
  if (start === null || end === null) return null;
  const [lo, hi] = start <= end ? [start, end] : [end, start];
  if (hi <= lo) return null;
  return { sourceStart: lo, sourceEnd: hi };
}

/**
 * The source offset of one selection endpoint: the endpoint's text node located verbatim in
 * its own `data-sourcepos` block's source slice, plus the in-node offset. `null` when the
 * node has no block, the block has no span, or the node's text does not occur in the slice.
 * The endpoint must be a text node -- an element endpoint counts children, not characters,
 * so treating its offset as one would report a wrong span. Omit, never report wrong.
 */
function sourceOffsetOfPoint(
  node: Node,
  offset: number,
  container: HTMLElement,
  source: string,
): number | null {
  if (!(node instanceof Text)) return null;
  const block = nearestSourcePosElement(node, container);
  if (!block) return null;
  const span = parseSourcePos(block.getAttribute(SOURCE_POS_ATTRIBUTE));
  if (!span) return null;
  const nodeText = node.textContent ?? "";
  if (nodeText.length === 0) return null;
  const index = source.slice(span.sourceStart, span.sourceEnd).indexOf(nodeText);
  if (index === -1) return null;
  return span.sourceStart + index + Math.min(offset, nodeText.length);
}

/**
 * The point just past a `Range`'s own end, from its last client rect -- where a small
 * affordance sits without covering the ranged text itself. `null` when the range paints no
 * rects (nothing visible to anchor to).
 */
export function rangeEndPosition(range: Range): SelectionPosition | null {
  const rects = range.getClientRects();
  if (rects.length === 0) return null;
  const last = rects[rects.length - 1];
  return { top: last.bottom, left: last.right };
}

/**
 * The point just past a selection's own end -- where a small "Add note" affordance sits
 * without covering the selected text itself. `null` when the selection has no range.
 */
export function selectionEndPosition(selection: Selection): SelectionPosition | null {
  if (selection.rangeCount === 0) return null;
  return rangeEndPosition(selection.getRangeAt(0));
}

export interface MarkdownViewProps {
  /** The file's source markdown. */
  source: string;
  /** Href for `[[target]]`; `null` renders a dead-link span instead of an anchor. */
  resolveWikilink?: (target: string) => string | null;
  /**
   * Fired on a non-empty text selection fully inside the view, with source offsets and the
   * viewport point just past its end, for an "Add note" affordance to anchor to.
   */
  onSelectSpan?: (sourceStart: number, sourceEnd: number, position: SelectionPosition) => void;
  className?: string;
}

export function MarkdownView({
  source,
  resolveWikilink,
  onSelectSpan,
  className,
}: MarkdownViewProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  // remarkWikilinks takes `source` as its plugin option via the [plugin, ...params] tuple
  // form — unified calls the attacher itself; passing an already-invoked transformer here
  // instead would have unified call *that* as the attacher, invoking it with no tree.
  const remarkPlugins = useMemo<PluggableList>(
    () => [remarkGfm, [remarkWikilinks, source], remarkSourcePos],
    [source],
  );
  const components = useMemo(
    () => ({ a: makeAnchorRenderer(resolveWikilink), table: TableWithScroll }),
    [resolveWikilink],
  );

  function handleSelectionEvent() {
    if (!onSelectSpan) return;
    const container = containerRef.current;
    if (!container) return;
    const selection = window.getSelection();
    if (!selection) return;
    const span = mapSelectionToSourceSpan(container, selection, source);
    if (!span) return;
    const position = selectionEndPosition(selection);
    if (position) onSelectSpan(span.sourceStart, span.sourceEnd, position);
  }

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: passively tracks text selection over the rendered markdown, not a click/key control.
    <div
      ref={containerRef}
      className={clsx("projects-prose", className)}
      onMouseUp={handleSelectionEvent}
      onKeyUp={handleSelectionEvent}
    >
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={REHYPE_PLUGINS}
        components={components}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
