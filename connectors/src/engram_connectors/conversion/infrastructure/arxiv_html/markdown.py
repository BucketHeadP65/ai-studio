"""ar5iv HTML to clean Markdown: stash math, run pandoc, restore math, strip structural chrome.

The author's exact LaTeX rides in each `<math alttext="...">` attribute, so math
is sanitised and stashed behind a NUL-delimited sentinel pandoc leaves untouched,
pandoc converts the surrounding HTML, and the sentinels are restored as `$...$`.
A cleaning pass then strips the ar5iv structural chrome pandoc preserved (residual
`<span>`/`<div>` wrappers, cross-ref anchors, page header/footer, ACM/author
metadata) and unwraps numeric-only math typography back to plain text.
"""

from __future__ import annotations

import html as html_module
import re

from typing import TYPE_CHECKING

import pypandoc

from engram.sources.contracts import ConversionError

from .tables import normalise_tables

if TYPE_CHECKING:
    from collections.abc import Callable

# The pandoc extensions switched off: heading and span attributes (no `{.ltx_*}` syntax), every table form but
# pipe tables, superscript and subscript (footnote marks come out as `<sup>x</sup>`), and escaped line breaks
# (a hard break ends in two spaces).
_PANDOC_TO = (
    "markdown-fenced_divs-bracketed_spans-native_divs-native_spans-header_attributes-link_attributes"
    "-grid_tables-multiline_tables-simple_tables-superscript-subscript"
    "-fenced_code_attributes-inline_code_attributes-escaped_line_breaks"
)
_PANDOC_ARGS = ["--wrap=none", "--markdown-headings=atx"]

# Stash math behind a NUL-delimited sentinel pandoc leaves untouched, then restore as `$...$`.
_MATH_TAG = re.compile(r"<math\b[^>]*?\balttext=\"([^\"]*)\"[^>]*?>.*?</math>", re.DOTALL | re.IGNORECASE)
_SENTINEL = "\x00MATH{}\x00"
SENTINEL_RE = re.compile(r"\x00MATH(\d+)\x00")
"""Matches a math sentinel and captures its index."""

# Some authors draw TikZ boxes *inside* math (algorithm pseudocode operators), the alttext then
# carries the raw multi-line \pgfpicture source, which no Markdown math engine can render. The blob
# is replaced by its drawn label as \text{...}, whitespace in stashed math is collapsed so an
# inline `$...$` span can never break across lines.
_PGF_BLOB = re.compile(
    r"\\raisebox\{[^{}]*\}\{\s*\\hbox to[\d.]+pt\{\\vbox to[\d.]+pt\{\\pgfpicture.*?\\endpgfpicture\}\}\}"
    r"|\\hbox to[\d.]+pt\{\\vbox to[\d.]+pt\{\\pgfpicture.*?\\endpgfpicture\}\}",
    re.DOTALL,
)
_PGF_LABEL = re.compile(r"([A-Za-z][A-Za-z0-9 _-]*[A-Za-z0-9])\}{4}")
_PGF_LABEL_STOPWORDS = frozenset({"rgb", "named", "pt", "hss", "gray"})
# An unescaped % in alttext is a LaTeX line-continuation: it comments out the line's remainder, so
# the % and the newline (plus the next line's indent) must vanish together or the formula truncates.
_MATH_CONTINUATION = re.compile(r"(?<!\\)%\s*\n\s*")
_MATH_DOLLAR_MACRO = re.compile(r"\\(?:mathdollar|textdollar)\b\s*")
_MATH_SPACING_NOOP = re.compile(r"\\(?:leavevmode|nobreak)\b")
# Literal author-note macros the AAAI/ACM classes leave unexpanded in the author block.
_LEAKED_MACRO = re.compile(r"\\equalcontrib\b")
# ar5iv glues author footnotes into the author line as a duplicated mark, <sup>X</sup><sup>X</sup>
# optionally followed by the mark again and/or a literal "footnotetext:"/"thanks:" keyword, then the
# note's text. The marks and keyword are chrome, the note text stays. A single <sup> (a real
# affiliation marker) never matches: the unmerged form requires the duplicate, and the merged form
# (pandoc fuses adjacent sups into <sup>1,2††</sup>) requires the keyword.
_AUTHOR_NOTE_MARK = re.compile(r"<sup>([^<]{1,8})</sup>(?:<sup>\1</sup>)+\1?\s*(?:(?:footnotetext|thanks):\s*)?")
_AUTHOR_NOTE_MERGED = re.compile(r"<sup>([^<]*?)(?:†{2,}|∗{2,}|(\d)\2+)</sup>\s*(?:footnotetext|thanks):\s*")

# A fenced code block in the converted Markdown. LaTeX verbatim renders the backtick as a
# typographic left quote (U+2018), so code samples that *contain* Markdown fences arrive as
# ‘‘‘json, restored to real backticks, with the enclosing fence lengthened so the inner fence
# can't terminate the block early.
_CODE_FENCE = re.compile(r"^(?P<fence>`{3,})(?P<info>[^\n]*)\n(?P<body>.*?)\n(?P=fence)[ \t]*$", re.MULTILINE | re.DOTALL)
_BACKTICK_RUN = re.compile(r"`+")

# Residual ar5iv structural markup survives pandoc (we disabled the div/span extensions), strip the
# `<span>`/`<div>` wrappers and `<a>` cross-ref anchors by tag name, keeping their inner text. Tables
# and figures are deliberately excluded so legitimate content survives untouched.
_CHROME_TAG = re.compile(r"</?(?:span|div|a)\b[^>]*>", re.IGNORECASE)
# Everything before the first top-level `# ` heading (the title) is ar5iv chrome: the "Report ... Issue"
# widget and the nav table-of-contents. Defensive: if no such heading exists, the text is left unchanged.
_TITLE_HEADING = re.compile(r"^# ", re.MULTILINE)
# Runs of 3+ newlines (left behind by tag removal) collapse to a single blank line.
_BLANK_RUN = re.compile(r"\n{3,}")

# An itemize/enumerate item's tag-span (the literal "•" / "1." glyph). Redundant with the Markdown
# list marker pandoc emits, so it is stripped from the HTML before conversion. Allows one level of
# nested span (enumerate tags wrap their number in an inner ltx_text span).
_ITEM_TAG = re.compile(r'<span class="ltx_tag ltx_tag_item">(?:<span[^>]*>[^<]*</span>|[^<])*</span>')
# ACM conference frontmatter (journalyear/doi/isbn/ccs/...) survives pandoc as a run of
# dagger-marked key:value segments, `<sup>†</sup>key: ...` (or legacy `^†^key: ...`).
# Each segment ends at the next dagger mark or end of line.
_ACM_NOTE = re.compile(
    r"(?:\^†+\^|<sup>†+</sup>)+\s*(?:journalyear|conference|booktitle|doi|isbn|submissionid|ccs|copyright|price):"
    r".*?(?=\^†+\^|<sup>†+</sup>|\n|$)"
)

# ar5iv page chrome stripped before pandoc runs. The <footer> ("Generated on ... by LaTeXML" plus the
# LaTeXML logo) would otherwise contribute a junk footer line and a junk logo asset. <figure>/<figcaption>
# wrappers have no Markdown representation, which forces pandoc's writer into raw-HTML fallback for
# everything inside them (every table, most notably), unwrapping them lets tables convert to pipe
# Markdown and turns each caption into a plain paragraph.
_FOOTER_TAG = re.compile(r"<footer\b.*?</footer>", re.DOTALL | re.IGNORECASE)
_FIGURE_TAG = re.compile(r"</?figure\b[^>]*>", re.IGNORECASE)
_FIGCAPTION_OPEN = re.compile(r"<figcaption\b[^>]*>", re.IGNORECASE)
_FIGCAPTION_CLOSE = re.compile(r"</figcaption>", re.IGNORECASE)

# pandoc's HTML reader only maps `<pre><code>` to a CodeBlock. A bare `<pre>` becomes flowing
# inline text (newlines collapsed, content re-escaped). ar5iv emits verbatim as a bare
# `<pre class="ltx_verbatim">`, so its content is wrapped in `<code>` before pandoc runs. The
# pre's classes then ride the CodeBlock and the fence and indent passes below see a real code block.
_BARE_PRE = re.compile(r"(<pre\b[^>]*>)(?!\s*<code\b)(.*?)(</pre>)", re.DOTALL | re.IGNORECASE)

# Numeric-only inline math is LaTeX typography, not mathematics: ar5iv wraps every table number in
# <math> (`$48.6$`), with \mathbf marking best results and \underline second-best. Unwrapped to plain
# text / ** / * so mirrors render without a math engine and the text layer stays searchable. Genuine
# math (anything beyond a bare number) keeps its `$...$`.
_NUMERIC_MATH = re.compile(
    r"(?<!\\)\$\s*(?:\\(?P<command>mathbf|boldsymbol|bm|textbf|underline|uline|mathrm|mathit|text)\s*\{)?"
    r"(?P<number>[-−+]?\d[\d,.]*\s*(?:\\%|%)?)"
    r"(?(command)\})\s*\$"
)
_BOLD_COMMANDS = frozenset({"mathbf", "boldsymbol", "bm", "textbf"})
_UNDERLINE_COMMANDS = frozenset({"underline", "uline"})

# The ACM \Description accessibility macro leaks as a literal token into figure prose, either as
# a token-only raw-HTML paragraph (inside <figure> blocks pandoc keeps as HTML) or at line start.
_DESCRIPTION_MACRO = re.compile(r"<p>\\+Description\b\s*</p>\n?|^\\+Description\b\s*", re.MULTILINE)


def _strip_accidental_indent(markdown: str) -> str:
    """Unindent paragraph lines that 4+ leading spaces would turn into code blocks, keeping code and list indents."""
    fence_length = 0
    previous = ""
    lines = markdown.split("\n")
    for index, original in enumerate(lines):
        line = original
        fence = re.match(r"^(`{3,})", line)
        if fence:
            if fence_length == 0:
                fence_length = len(fence.group(1))
            elif len(fence.group(1)) >= fence_length:
                fence_length = 0
        elif fence_length == 0 and re.match(r"^ {4,}\S", line):
            list_context = bool(re.match(r"^\s*(?:[-*+]|\d+\.)\s", previous)) or previous.startswith("    ")
            if not list_context:
                line = line.lstrip(" ")
                lines[index] = line
        if line.strip():
            previous = line
    return "\n".join(lines)


def _normalise_code_fences(markdown: str) -> str:
    """Restore backticks LaTeX verbatim mangled to U+2018 inside code blocks, keep fences sound."""

    def fix(match: re.Match[str]) -> str:
        body = match.group("body").replace("‘", "`")
        longest_run = max((len(run) for run in _BACKTICK_RUN.findall(body)), default=0)
        fence = "`" * max(3, longest_run + 1)
        # pandoc surfaces the block's first ltx_* class as the info string, not a real language.
        info = match.group("info")
        if info.strip().startswith("ltx_"):
            info = ""
        return f"{fence}{info}\n{body}\n{fence}"

    return _CODE_FENCE.sub(fix, markdown)


def _restore_math(converted: str, stash: list[str]) -> str:
    """Replace math sentinels with `$...$`, escaping `|` on pipe-table lines so cells survive."""
    if "\x00" not in converted:
        return converted

    def restore_line(line: str) -> str:
        in_table = line.lstrip().startswith("|")

        def replace(match: re.Match[str]) -> str:
            latex = stash[int(match.group(1))]
            return f"${latex.replace('|', '\\|')}$" if in_table else f"${latex}$"

        return SENTINEL_RE.sub(replace, line)

    return "\n".join(restore_line(line) for line in converted.split("\n"))


def _unwrap_numeric_math(match: re.Match[str]) -> str:
    r"""Unwrap `$48.6$` to `48.6`, with `\mathbf` (best result) as bold and `\underline` (second best) as italic."""
    number = match.group("number").replace("\\%", "%").strip()
    command = match.group("command")
    if command in _BOLD_COMMANDS:
        return f"**{number}**"
    if command in _UNDERLINE_COMMANDS:
        return f"*{number}*"
    return number


def _strip_pgf_blob(match: re.Match[str]) -> str:
    r"""Replace one in-math \pgfpicture blob with its drawn label (`\text{Tri-Retrieve}`)."""
    label = None
    for candidate in _PGF_LABEL.findall(match.group(0)):
        if candidate.strip().lower() not in _PGF_LABEL_STOPWORDS:
            label = candidate.strip()
    return f"\\text{{{label}}}" if label else "\\square"


def _sanitise_math(latex: str) -> str:
    """Make one alttext renderable as inline markdown math."""
    latex = _MATH_CONTINUATION.sub("", latex)
    if "\\pgfpicture" in latex:
        latex = _PGF_BLOB.sub(_strip_pgf_blob, latex)
    latex = _MATH_DOLLAR_MACRO.sub(r"\\$", latex)
    latex = _MATH_SPACING_NOOP.sub("", latex)
    return re.sub(r"\s+", " ", latex).strip()


def _drop_leading_boilerplate(markdown: str) -> str:
    """Drop everything before the first top-level `# ` heading, leave the text as-is when none exists."""
    match = _TITLE_HEADING.search(markdown)
    return markdown[match.start() :] if match is not None else markdown


def _run_pandoc(html: str) -> str:
    """Convert HTML to markdown with the bundled pandoc binary and this module's pandoc options."""
    return pypandoc.convert_text(html, to=_PANDOC_TO, format="html", extra_args=_PANDOC_ARGS)


def html_to_markdown(html: str, *, pandoc: Callable[[str], str] = _run_pandoc) -> tuple[str, list[str]]:
    """Convert ar5iv HTML to markdown: stash math, run pandoc, restore math, strip structural chrome.

    Math inside base64-embedded SVGs is still a sentinel in the returned markdown.
    Restore it from the returned stash when the SVG is decoded.

    Args:
        html: The ar5iv page's HTML.
        pandoc: Converts the prepared HTML to markdown. The default runs the bundled pandoc binary.

    Returns:
        The markdown and the math stash its sentinels index into.

    Raises:
        ConversionError: pandoc is missing or exits with an error.
    """
    stash: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        stash.append(_sanitise_math(html_module.unescape(match.group(1))))
        return _SENTINEL.format(len(stash) - 1)

    prepared = _ITEM_TAG.sub("", _MATH_TAG.sub(_stash, html))
    prepared = _FOOTER_TAG.sub("", prepared)
    prepared = _FIGURE_TAG.sub("", prepared)
    prepared = _FIGCAPTION_CLOSE.sub("</p>", _FIGCAPTION_OPEN.sub("<p>", prepared))
    prepared = _BARE_PRE.sub(r"\1<code>\2</code>\3", prepared)
    prepared = normalise_tables(prepared)
    try:
        converted = pandoc(prepared)
    except (RuntimeError, OSError) as error:
        msg = f"pandoc could not convert the arXiv HTML: {error}"
        raise ConversionError(msg) from error
    restored = _restore_math(converted, stash)
    cleaned = _ACM_NOTE.sub("", _CHROME_TAG.sub("", restored))
    cleaned = _DESCRIPTION_MACRO.sub("", cleaned)
    cleaned = _LEAKED_MACRO.sub("", cleaned)
    cleaned = _AUTHOR_NOTE_MARK.sub(" ", cleaned)
    cleaned = _AUTHOR_NOTE_MERGED.sub(lambda m: f"<sup>{m.group(1)}</sup> " if m.group(1) else " ", cleaned)
    cleaned = _NUMERIC_MATH.sub(_unwrap_numeric_math, cleaned)
    cleaned = _normalise_code_fences(cleaned)
    cleaned = _strip_accidental_indent(cleaned)
    body = _drop_leading_boilerplate(cleaned)
    return _BLANK_RUN.sub("\n\n", body).strip(), stash
