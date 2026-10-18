"""Rebuild LaTeXML tables into a form pandoc can express as a pipe table.

pandoc only emits a pipe table when every row has plain inline-content cells, so
LaTeXML's span and nesting tricks are flattened in HTML before pandoc runs:
row/colspans are expanded into a rectangular grid, nested layout tables collapse
into their cell, and single-column boxes unwrap to block content.
"""

from __future__ import annotations

import re

from .spans import break_paragraph_spans

# _INNER_TABLE matches a table containing no nested <table> (innermost-first, outer tables become
# innermost on the next pass). The data-normalised marker keeps already-rewritten tables out of
# later passes, pandoc drops the attribute.
_INNER_TABLE = re.compile(r"<table\b[^>]*>(?:(?!<table\b).)*?</table>", re.DOTALL | re.IGNORECASE)
_NORMALISED_MARK = "data-normalised"
_TABLE_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TABLE_CELL = re.compile(r"<(td|th)\b([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_SPAN_ATTR = re.compile(r"\b(rowspan|colspan)=\"(\d+)\"", re.IGNORECASE)
# Block wrappers, lists and line breaks inside a cell all become spaces: a pipe-table cell is
# single-line inline content by definition, and pandoc falls back to raw HTML for the whole table
# if any cell keeps a block element or LineBreak.
_CELL_BLOCK_TAG = re.compile(r"</?(?:p|div|br|ul|ol|li)\b[^>]*>", re.IGNORECASE)


def _flatten_cell(content: str) -> str:
    """Reduce a table cell to single-line inline content: block wrappers and line breaks become spaces."""
    return re.sub(r"\s+", " ", _CELL_BLOCK_TAG.sub(" ", content)).strip()


def _table_grid(table_html: str) -> list[list[str]]:
    """Resolve a table's rows into a rectangular grid of raw cell contents, with spanned slots left empty."""
    rows: list[dict[int, str]] = []
    pending: dict[int, dict[int, str]] = {}
    for index, row_match in enumerate(_TABLE_ROW.finditer(table_html)):
        row: dict[int, str] = pending.pop(index, {})
        column = 0
        for cell_match in _TABLE_CELL.finditer(row_match.group(1)):
            spans = {key.lower(): int(value) for key, value in _SPAN_ATTR.findall(cell_match.group(2))}
            while column in row:
                column += 1
            row[column] = cell_match.group(3)
            for covered in range(column + 1, column + spans.get("colspan", 1)):
                row[covered] = ""
            for later in range(index + 1, index + spans.get("rowspan", 1)):
                for covered in range(column, column + spans.get("colspan", 1)):
                    pending.setdefault(later, {})[covered] = ""
            column += spans.get("colspan", 1)
        rows.append(row)
    width = max((max(row) + 1 for row in rows if row), default=0)
    return [[row.get(column, "") for column in range(width)] for row in rows]


def normalise_tables(html: str) -> str:
    """Rewrite every LaTeXML table, innermost first, into a form pandoc can express as a pipe table.

    Args:
        html: The paper's HTML.

    Returns:
        The HTML with every table rewritten.
    """
    while True:
        changed = False
        pieces: list[str] = []
        cursor = 0
        for match in _INNER_TABLE.finditer(html):
            if _NORMALISED_MARK in match.group(0)[: match.group(0).index(">")]:
                continue
            # <th counts <thead too, but symmetrically on both sides (</th counts </thead), so the
            # open/close comparison stays balanced. A miss here is sticky: the nested table would be
            # normalised standalone and the outer table could then never match _INNER_TABLE again.
            opens = html.count("<td", 0, match.start()) + html.count("<th", 0, match.start())
            closes = html.count("</td", 0, match.start()) + html.count("</th", 0, match.start())
            inside_cell = opens > closes
            grid = _table_grid(match.group(0))
            filled = [cell for row in grid for cell in row if cell.strip()]
            width = max((len(row) for row in grid), default=0)
            if inside_cell:
                replacement = " ".join(_flatten_cell(cell) for cell in filled)
            elif width <= 1 or len(filled) <= 1:
                # Real <p> wrappers, not blank lines: a row's content is usually inline spans, and
                # pandoc's HTML reader merges inline runs across blank lines into one paragraph.
                rows = [f"<p>{break_paragraph_spans(cell)}</p>" for cell in filled]
                replacement = "\n" + "\n".join(rows) + "\n" if rows else "\n"
            else:
                body = "".join(
                    "<tr>" + "".join(f"<td>{_flatten_cell(cell)}</td>" for cell in row) + "</tr>"
                    for row in grid
                    if any(cell.strip() for cell in row)
                )
                replacement = f'<table {_NORMALISED_MARK}="1">{body}</table>'
            pieces.extend((html[cursor : match.start()], replacement))
            cursor = match.end()
            changed = True
        if not changed:
            return html
        pieces.append(html[cursor:])
        html = "".join(pieces)
