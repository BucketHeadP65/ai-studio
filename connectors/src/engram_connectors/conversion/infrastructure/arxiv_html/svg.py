"""Repair LaTeXML's HTML-inline SVGs and decode the `data:` URIs they ride in.

LaTeXML renders TikZ pictures as inline SVG embedded in the page as a `data:` URI:
namespace implied, names HTML-lowercased, and label text in a `<foreignObject>`
positioned via an SVG2 transform plus ar5iv stylesheet variables, none of which
hold in a standalone file. This module decides whether such a picture is an inline
text decoration (folded into prose), a text box (folded into a blockquote), or a
genuine diagram (rewritten into a self-contained `.svg` asset), and repairs the
ones kept as assets so they render outside ar5iv.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html as html_module
import re
import textwrap

from urllib.parse import unquote_to_bytes

from core.observability import get_logger

from .fetch import ASSETS_DIR
from .markdown import SENTINEL_RE
from .spans import break_paragraph_spans

_logger = get_logger(__name__)

# LaTeXML embeds TikZ/diagram pictures as inline `data:` URIs rather than hosted files.
_DATA_URI = re.compile(r"^data:(?P<mime>[^;,]+)?(?P<params>(?:;[^,]*)*),(?P<payload>.*)$", re.DOTALL)
# The longest label, in characters, of a mid-sentence SVG that folds into prose as bold text.
_INLINE_LABEL_MAX = 80
# A block-level SVG with no drawing primitives, at most `_TEXT_BOX_MAX_PATHS` <path>s and more than
# label-length text is a TikZ text box, whose text is extracted into the document.
_SVG_DRAWING_TAG = re.compile(r"<(?:image|line|circle|ellipse|rect|polygon|polyline|use)\b", re.IGNORECASE)
_SVG_PATH_TAG = re.compile(r"<path\b", re.IGNORECASE)
_TEXT_BOX_MAX_PATHS = 6
# Average serif glyph advance in em, the per-line character budget when wrapping foreignObject text into tspans.
_GLYPH_EM = 0.55
_MIME_EXTENSIONS = {"image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
# A <foreignObject> carrying TikZ label text as XHTML spans, which conversion replaces with a native SVG <text> element.
_FOREIGN_OBJECT = re.compile(r"<foreignObject\b(?P<attrs>[^>]*)>(?P<inner>.*?)</foreignObject>", re.DOTALL | re.IGNORECASE)
_FO_DIMENSION = re.compile(r"\b(width|height)=\"([\d.]+)\"", re.IGNORECASE)
_FO_TRANSFORM = re.compile(r"\btransform=\"([^\"]*)\"")
# The label height and depth CSS variables, as ar5iv (--fo_height) and current LaTeXML (--ltx-fo-height) write them.
_FO_EM = re.compile(r"--(?:ltx-)?fo[-_](height|depth)\s*:\s*([\d.]+)em")
_FONT_PERCENT = re.compile(r"font-size\s*:\s*([\d.]+)%")
_LINE_BREAK_TAG = re.compile(r"<br\b[^>]*/?>", re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
# HTML-void elements inside SVG label content: legal unclosed in HTML, fatal to an XML parser.
_VOID_TAG = re.compile(r"<(br|hr|wbr|img)\b([^>]*?)/?>")
# Root <svg> width/height, for deriving a viewBox when LaTeXML omitted one.
_SVG_DIMENSION = re.compile(r'\b(width|height)="([\d.]+)"')


def _restore_svg_sentinels(text: str, math_stash: list[str]) -> str:
    """Restore math sentinels that rode into a base64 SVG payload opaquely, as `$...$` text."""
    return SENTINEL_RE.sub(lambda m: f"${math_stash[int(m.group(1))]}$" if int(m.group(1)) < len(math_stash) else "", text)


def _svg_label_text(data: bytes, math_stash: list[str]) -> str | None:
    """The concatenated label text of an SVG, or `None` when it has none or is not UTF-8."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = _restore_svg_sentinels(text, math_stash)
    labels = []
    for match in _FOREIGN_OBJECT.finditer(text):
        label = re.sub(r"\s+", " ", html_module.unescape(_ANY_TAG.sub("", match.group("inner")))).strip()
        if label:
            labels.append(label)
    return " ".join(labels) or None


def _is_inline_context(markdown: str, reference: str) -> bool:
    """Whether `reference`'s (first) occurrence shares its line with other content, i.e. sits mid-prose."""
    index = markdown.find(reference)
    if index < 0:
        return False
    start = markdown.rfind("\n", 0, index) + 1
    end = markdown.find("\n", index)
    line = markdown[start : end if end >= 0 else len(markdown)]
    return line.strip() != reference.strip()


def _foreign_object_lines(inner: str) -> list[str]:
    """The visible text lines of one foreignObject: `ltx_p` paragraph spans and `<br>`s break lines."""
    lines = []
    for segment in _LINE_BREAK_TAG.split(break_paragraph_spans(inner)):
        text = re.sub(r"\s+", " ", html_module.unescape(_ANY_TAG.sub("", segment))).strip()
        if text:
            lines.append(text)
    return lines


def _escape_svg_prose(line: str) -> str:
    """Escape markdown-active characters, angle brackets above all, in prose lifted out of an SVG text box."""
    line = line.replace("\\", "\\\\").replace("<", "\\<").replace(">", "\\>")
    return f"\\{line}" if line.startswith("#") else line


def _svg_text_block(data: bytes, math_stash: list[str]) -> str | None:
    """A block-level SVG that is purely styled text as a markdown blockquote, or `None` for a diagram."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if _SVG_DRAWING_TAG.search(text) or len(_SVG_PATH_TAG.findall(text)) > _TEXT_BOX_MAX_PATHS:
        return None
    text = _restore_svg_sentinels(text, math_stash)
    blocks = [lines for match in _FOREIGN_OBJECT.finditer(text) if (lines := _foreign_object_lines(match.group("inner")))]
    if sum(len(line) for lines in blocks for line in lines) <= _INLINE_LABEL_MAX:
        return None
    quoted = []
    for index, lines in enumerate(blocks):
        escaped = [_escape_svg_prose(line) for line in lines]
        if index == 0 and len(blocks) > 1 and len(escaped) == 1 and len(lines[0]) <= _INLINE_LABEL_MAX:
            escaped = [f"**{escaped[0]}**"]
        quoted.append("  \n".join(f"> {line}" for line in escaped))
    return "\n>\n".join(quoted)


def _foreign_object_to_text(match: re.Match[str]) -> str:
    """Replace one `<foreignObject>` label with an equivalent native SVG `<text>` element."""
    attrs, inner = match.group("attrs"), match.group("inner")
    dimensions = {key.lower(): float(value) for key, value in _FO_DIMENSION.findall(attrs)}
    height = dimensions.get("height", 10.0)
    extents = {key: float(value) for key, value in _FO_EM.findall(attrs)}
    above, below = extents.get("height", 0.0), extents.get("depth", 0.0)
    baseline = height * (above / (above + below)) if above + below else height * 0.8
    em_units = height / (above + below) if above + below else 10.0
    percent = _FONT_PERCENT.search(inner)
    font_size = em_units * (float(percent.group(1)) / 100.0 if percent else 1.0)
    transform_match = _FO_TRANSFORM.search(attrs)
    transform = transform_match.group(1) if transform_match else f"matrix(1 0 0 -1 0 {height})"
    width = dimensions.get("width", 0.0)
    budget = max(20, int(width / (font_size * _GLYPH_EM))) if width and font_size else 0

    lines = []
    for segment in _foreign_object_lines(inner):
        pieces = textwrap.wrap(segment, budget) if budget and len(segment) > _INLINE_LABEL_MAX else [segment]
        lines.extend(piece.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") for piece in pieces)
    if not lines:
        return ""
    spans = "".join(f'<tspan x="0" dy="{font_size * 1.2:.2f}">{line}</tspan>' for line in lines[1:])
    return (
        f'<text x="0" y="{baseline:.2f}" transform="{transform}" font-size="{font_size:.2f}"'
        f' font-family="serif" stroke="none">{lines[0]}{spans}</text>'
    )


def _repair_svg(data: bytes, math_stash: list[str]) -> bytes:
    """Make a LaTeXML HTML-inline SVG valid as a standalone `.svg` file, leaving non-UTF-8 data untouched."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    text = _restore_svg_sentinels(text, math_stash)
    text = _FOREIGN_OBJECT.sub(_foreign_object_to_text, text)
    root_tag = text.split(">", 1)[0]
    if "xmlns=" not in root_tag:
        text = text.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    text = re.sub(r"\bviewbox=", "viewBox=", text)
    if "viewBox=" not in text.split(">", 1)[0]:
        dimensions = dict(_SVG_DIMENSION.findall(text.split(">", 1)[0]))
        if "width" in dimensions and "height" in dimensions:
            text = text.replace("<svg", f'<svg viewBox="0 0 {dimensions["width"]} {dimensions["height"]}"', 1)
    return _VOID_TAG.sub(r"<\1\2/>", text).encode("utf-8")


def _decode_data_uri(src: str) -> tuple[bytes, str] | None:
    """Decode an inline `data:` URI image to `(bytes, extension)`, or `None` when malformed or empty."""
    match = _DATA_URI.match(src)
    if match is None:
        return None
    extension = _MIME_EXTENSIONS.get((match.group("mime") or "").strip().lower(), "bin")
    payload = match.group("payload")
    try:
        if ";base64" in (match.group("params") or ""):
            data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
        else:
            data = unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None
    return (data, extension) if data else None


def resolve_data_reference(
    full: str, src: str, markdown: str, identifier: str, math_stash: list[str]
) -> tuple[str, tuple[str, bytes] | None] | None:
    """Resolve one `data:` image reference to `(replacement, optional (asset_key, bytes))`, or `None` if undecodable.

    Args:
        full: The whole image reference as it appears in the markdown.
        src: The `data:` URI inside the reference.
        markdown: The paper's markdown, read to tell an inline image from a block one.
        identifier: The arXiv paper id.
        math_stash: The paper's math expressions, indexed by the sentinels that sit in the image text.

    Returns:
        The text that replaces `full`, with the asset to save when the image becomes a file, or `None` when the URI cannot be decoded.
    """
    decoded = _decode_data_uri(src)
    if decoded is None:
        _logger.warning("Undecodable data: URI image in {identifier}. Leaving the reference unchanged", identifier=identifier)
        return None
    data, extension = decoded
    if extension == "svg":
        if _is_inline_context(markdown, full):
            label = _svg_label_text(data, math_stash)
            if label is not None and len(label) <= _INLINE_LABEL_MAX:
                return f"**{label}**", None
        else:
            block = _svg_text_block(data, math_stash)
            if block is not None:
                return block, None
    name = f"inline-{hashlib.sha256(data).hexdigest()[:8]}.{extension}"
    if extension == "svg":
        data = _repair_svg(data, math_stash)
    key = f"{ASSETS_DIR}/{identifier}/{name}"
    return f"![]({key})", (key, data)
