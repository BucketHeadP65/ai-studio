// Maps a file's source positions onto the elements a rendered MarkdownView put on
// screen. Every rendered node carries `data-sourcepos="start-end"` (character offsets
// into the same source string), which is the only handle a sibling component has on
// "where did line N end up".

const SOURCE_POS_ATTRIBUTE = "data-sourcepos";
const SOURCE_POS_PATTERN = /^(\d+)-(\d+)$/;

interface PositionedElement {
  element: HTMLElement;
  start: number;
  end: number;
}

/**
 * Character offset where a 1-based `line` starts in `source`. Line 1 starts at 0; a
 * line past the end of the source clamps to the source's length.
 */
export function lineStartOffset(source: string, line: number): number {
  if (line <= 1) return 0;
  let offset = 0;
  for (let remaining = line - 1; remaining > 0; remaining -= 1) {
    const newline = source.indexOf("\n", offset);
    if (newline === -1) return source.length;
    offset = newline + 1;
  }
  return offset;
}

function positionedElements(container: HTMLElement): PositionedElement[] {
  const found: PositionedElement[] = [];
  for (const element of container.querySelectorAll<HTMLElement>(`[${SOURCE_POS_ATTRIBUTE}]`)) {
    const match = SOURCE_POS_PATTERN.exec(element.getAttribute(SOURCE_POS_ATTRIBUTE) ?? "");
    if (!match) continue;
    found.push({ element, start: Number(match[1]), end: Number(match[2]) });
  }
  return found;
}

/**
 * The rendered element for a source `offset`: the narrowest one whose `data-sourcepos`
 * range covers it, or, when the offset falls in a gap the renderer dropped (a blank
 * line, say), the first element that starts after it. `null` when neither exists.
 */
export function elementCoveringOffset(container: HTMLElement, offset: number): HTMLElement | null {
  let covering: PositionedElement | null = null;
  let following: PositionedElement | null = null;

  for (const candidate of positionedElements(container)) {
    if (candidate.start <= offset && offset < candidate.end) {
      if (covering === null || candidate.end - candidate.start < covering.end - covering.start) {
        covering = candidate;
      }
      continue;
    }
    if (candidate.start > offset && (following === null || candidate.start < following.start)) {
      following = candidate;
    }
  }

  return (covering ?? following)?.element ?? null;
}

/**
 * Whether `element` has been laid out at all. jsdom reports every box as zero-sized, so
 * a measurement-driven position has to fall back to arithmetic there.
 */
export function hasLayout(element: HTMLElement): boolean {
  return element.getBoundingClientRect().height > 0;
}

/**
 * The covering `data-sourcepos` element(s) for the span `[start, end)` -- one when both
 * endpoints land in the same block, up to two (the start's and the end's) otherwise. Used
 * to carry the pointer-cursor and click affordance on a note's anchored span, independent
 * of whether the exact characters can be highlighted.
 */
export function blocksForSourceSpan(
  container: HTMLElement,
  start: number,
  end: number,
): HTMLElement[] {
  if (end <= start) return [];
  const startBlock = elementCoveringOffset(container, start);
  const endBlock = elementCoveringOffset(container, end - 1);
  const blocks = [startBlock, endBlock].filter(
    (element): element is HTMLElement => element !== null,
  );
  return [...new Set(blocks)];
}

interface SourcePosPoint {
  node: Text;
  offset: number;
}

/**
 * The point inside `block` -- a text node plus an in-node offset -- for the block-relative
 * source `offset`, by walking `block`'s own text nodes in document order and locating each
 * one's own slice of `blockSource` (advancing past every node already matched, so a repeated
 * run of text is never re-matched against an earlier position). `null` when no text node's
 * slice covers `offset` -- it fell inside markdown syntax the renderer dropped rather than
 * rendered as raw text (an emphasis marker, an escape).
 */
function textNodePoint(block: Element, blockSource: string, offset: number): SourcePosPoint | null {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  let cursor = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent ?? "";
    if (text.length === 0) continue;
    const index = blockSource.indexOf(text, cursor);
    if (index === -1) continue;
    const nodeStart = index;
    const nodeEnd = index + text.length;
    if (offset >= nodeStart && offset <= nodeEnd) {
      return { node: node as Text, offset: offset - nodeStart };
    }
    cursor = nodeEnd;
  }
  return null;
}

/**
 * The DOM `Range` covering source characters `[start, end)` -- the inverse of the
 * `data-sourcepos` mapping `elementCoveringOffset` reads forward: each endpoint's covering
 * block is found, then walked text-node by text-node to the exact point. `null` when either
 * endpoint has no covering block or no mappable point inside it (the span crosses markdown
 * syntax the renderer didn't preserve as literal text) -- an untrustworthy range is omitted,
 * never reported wrong.
 */
export function rangeForSourceSpan(
  container: HTMLElement,
  start: number,
  end: number,
  source: string,
): Range | null {
  if (end <= start) return null;
  const startBlock = elementCoveringOffset(container, start);
  const endBlock = elementCoveringOffset(container, end - 1);
  if (!startBlock || !endBlock) return null;
  const startSpan = parseAttrSpan(startBlock);
  const endSpan = parseAttrSpan(endBlock);
  if (!startSpan || !endSpan) return null;
  const startPoint = textNodePoint(
    startBlock,
    source.slice(startSpan.start, startSpan.end),
    start - startSpan.start,
  );
  const endPoint = textNodePoint(
    endBlock,
    source.slice(endSpan.start, endSpan.end),
    end - endSpan.start,
  );
  if (!startPoint || !endPoint) return null;
  try {
    const range = document.createRange();
    range.setStart(startPoint.node, startPoint.offset);
    range.setEnd(endPoint.node, endPoint.offset);
    return range;
  } catch {
    return null;
  }
}

function parseAttrSpan(element: Element): { start: number; end: number } | null {
  const match = SOURCE_POS_PATTERN.exec(element.getAttribute(SOURCE_POS_ATTRIBUTE) ?? "");
  if (!match) return null;
  return { start: Number(match[1]), end: Number(match[2]) };
}

function nearestPositionedAncestor(node: Node, container: HTMLElement): Element | null {
  let current: Element | null = node instanceof Element ? node : node.parentElement;
  while (current && container.contains(current)) {
    if (current.hasAttribute(SOURCE_POS_ATTRIBUTE)) return current;
    current = current.parentElement;
  }
  return null;
}

/**
 * The character offset in `source` for the point `offset` inside DOM text node `node` --
 * `node`'s own covering `data-sourcepos` ancestor located by walking up from it, then
 * `node`'s text found verbatim inside that ancestor's source slice. `null` when `node`
 * isn't a text node, has no covering ancestor inside `container`, or its text doesn't
 * occur in the ancestor's slice (it fell inside markdown syntax the renderer dropped).
 * Tells apart a click landing on one of several notes anchored in the same block, the
 * way a real click leaves the browser's own selection collapsed right at that point.
 */
export function sourceOffsetOfDomPoint(
  container: HTMLElement,
  node: Node,
  offset: number,
  source: string,
): number | null {
  if (!(node instanceof Text)) return null;
  const block = nearestPositionedAncestor(node, container);
  if (!block) return null;
  const span = parseAttrSpan(block);
  if (!span) return null;
  const nodeText = node.textContent ?? "";
  if (nodeText.length === 0) return null;
  const index = source.slice(span.start, span.end).indexOf(nodeText);
  if (index === -1) return null;
  return span.start + index + Math.min(offset, nodeText.length);
}

/** A `[start, end)` character span, independent of where it came from. */
export interface OffsetSpan {
  start: number;
  end: number;
}

/**
 * The narrowest of `spans` whose `[start, end)` covers `offset` -- the same "most specific
 * wins" rule `elementCoveringOffset` applies to rendered elements, generalized to any spans.
 * Ties (equal width) keep the earlier entry. `null` when none covers it.
 */
export function narrowestCoveringSpan<T extends OffsetSpan>(spans: T[], offset: number): T | null {
  let best: T | null = null;
  for (const span of spans) {
    if (span.start > offset || offset >= span.end) continue;
    if (best === null || span.end - span.start < best.end - best.start) best = span;
  }
  return best;
}
