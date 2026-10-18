"""LaTeXML `ltx_p` inline-span handling shared by table flattening and SVG label extraction.

LaTeXML emits a paragraph inside a table cell or a picture as an `ltx_p` *span*
(an inline element), not a block. A single-column box or an extracted text box
would therefore join every paragraph into one wall of text without an explicit
break per span, so a hard break is inserted before each one.
"""

from __future__ import annotations

import re

# Attribute order varies: cells write class first, pictures id first.
_PARAGRAPH_SPAN = re.compile(r'<span\b[^>]*?\bclass="[^"]*\bltx_p\b[^"]*"[^>]*>')


def break_paragraph_spans(cell: str) -> str:
    """Put each ltx_p paragraph-span of an unwrapped single-column box on its own line.

    Args:
        cell: The HTML of one table cell.

    Returns:
        The cell with a line break before every paragraph span after the first.
    """
    broken = _PARAGRAPH_SPAN.sub(lambda match: f"<br/>{match.group(0)}", cell)
    return re.sub(r"^(\s*(?:<span\b[^>]*>\s*)*)<br/>", r"\1", broken, count=1)
