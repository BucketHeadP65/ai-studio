"""Unit tests for `ArxivHtmlConverter` and the `arxiv_id` URL parser.

No real network happens: HTTP is faked with `httpx.MockTransport`, whose
handler returns either a small ar5iv-style fixture HTML (200) or a 404. pandoc
runs for real on the in-test fixture string — the bundled `pypandoc-binary`
is deterministic and offline.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from engram.sources.contracts import ConversionError

from engram_connectors.conversion.contracts import ArxivHtmlConverter
from engram_connectors.conversion.infrastructure.arxiv_html.fetch import arxiv_id
from engram_connectors.conversion.infrastructure.arxiv_html.markdown import html_to_markdown

PDF_PATH = Path("ignored.pdf")  # the HTML route ignores this; never read from disk

FIXTURE_HTML = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<div class="ltx_para"><a class="ltx_ref" href="#">Report GitHub Issue</a></div>
<div class="ltx_TOC"><a class="ltx_ref" href="#S1">1 Intro</a> <a class="ltx_ref" href="#S2">2 Method</a></div>
<h1 class="ltx_title ltx_title_document"><span class="ltx_text">Sample</span> Paper</h1>
<div class="ltx_para"><h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">1 </span>Intro</h2></div>
<div class="ltx_para"><p>Let <math alttext="x^2" class="ltx_Math" display="inline"><semantics><mrow><msup><mi>x</mi><mn>2</mn></msup></mrow></semantics></math> be a value, see <a class="ltx_ref" href="#S2">Section 2</a> for details.</p></div>
<figure class="ltx_figure"><figcaption>Figure 1: a caption.</figcaption></figure>
<table><tr><td>cell</td></tr></table>
</div>
</body></html>"""


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://arxiv.org/abs/2412.15605", "2412.15605"),
        ("https://arxiv.org/abs/2412.15605v2", "2412.15605"),
        ("https://arxiv.org/pdf/2412.15605", "2412.15605"),
        ("https://arxiv.org/html/2412.15605v1", "2412.15605"),
        ("https://doi.org/10.48550/arXiv.2412.15605", "2412.15605"),
    ],
)
def test_arxiv_id_parses_known_url_shapes(url: str, expected: str) -> None:
    assert arxiv_id(url) == expected


@pytest.mark.parametrize("url", [None, "https://example.com/x", "https://arxiv.org/about"])
def test_arxiv_id_returns_none_for_non_arxiv(url: str | None) -> None:
    assert arxiv_id(url) is None


async def test_to_markdown_raises_when_source_url_is_none() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=FIXTURE_HTML)

    converter = ArxivHtmlConverter(client=_client(handler))

    with pytest.raises(ConversionError):
        await converter.to_markdown(PDF_PATH, source_url=None)
    assert calls == []  # no fetch when there is no arXiv id


async def test_to_markdown_raises_for_non_arxiv_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return pytest.fail("non-arXiv URL must not trigger a fetch")

    converter = ArxivHtmlConverter(client=_client(handler))

    with pytest.raises(ConversionError):
        await converter.to_markdown(PDF_PATH, source_url="https://example.com/x")


async def test_to_markdown_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    converter = ArxivHtmlConverter(client=_client(handler))

    with pytest.raises(ConversionError):
        await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")


@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout])
async def test_to_markdown_raises_conversion_error_when_the_fetch_fails(error_type: type[httpx.TransportError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "arXiv did not answer"
        raise error_type(msg, request=request)

    converter = ArxivHtmlConverter(client=_client(handler))

    with pytest.raises(ConversionError, match=r"2412\.15605"):
        await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")


@pytest.mark.parametrize("error", [RuntimeError('Pandoc died with exitcode "1" during conversion'), OSError("No pandoc was found")])
def test_html_to_markdown_raises_conversion_error_when_pandoc_fails(error: Exception) -> None:
    def failing_pandoc(html: str) -> str:
        raise error

    with pytest.raises(ConversionError, match="pandoc"):
        html_to_markdown(FIXTURE_HTML, pandoc=failing_pandoc)


async def test_to_markdown_preserves_math_and_cleans_spans() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, text=FIXTURE_HTML)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605v2")

    assert captured == ["https://arxiv.org/html/2412.15605"]
    assert result.page_count == 0
    assert result.converter == "arxiv-html/pandoc"
    assert "$x^2$" in result.markdown
    assert "## " in result.markdown
    assert "Intro" in result.markdown
    assert "<span" not in result.markdown
    assert "{.ltx" not in result.markdown
    assert "ltx_tag" not in result.markdown


async def test_to_markdown_strips_div_anchor_chrome_and_leading_boilerplate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FIXTURE_HTML)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    # ar5iv structural chrome is gone: no div wrappers, no anchor tags, no spans.
    assert "<div" not in markdown
    assert "<a " not in markdown
    assert "<span" not in markdown
    # Leading widget + nav TOC dropped: the document starts at the title heading.
    assert markdown.startswith("# Sample Paper")
    assert "Report GitHub Issue" not in markdown
    assert "Method" not in markdown  # the TOC entry, not a real section
    # Legitimate content survives: table cell, figure caption, cross-ref text, math.
    assert "cell" in markdown
    assert "Figure 1: a caption." in markdown
    assert "Section 2" in markdown
    assert "$x^2$" in markdown
    # Blank-line runs left by the removals are collapsed (never 3+ newlines in a row).
    assert "\n\n\n" not in markdown


async def test_to_markdown_strips_list_item_glyphs_and_acm_metadata_notes() -> None:
    # Two ar5iv noise shapes observed in real mirrors: itemize items carry a literal bullet glyph in a
    # tag-span (pandoc then emits "- •"), and ACM conference frontmatter lands as a dagger-marked
    # key/value run (^†^^†^journalyear: ... ^†^^†^ccs: ...).
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Noise Paper</h1>
<div class="ltx_para"><p><sup class="ltx_note_mark">†</sup><sup class="ltx_note_mark">†</sup>journalyear: 2025<sup class="ltx_note_mark">†</sup><sup class="ltx_note_mark">†</sup>conference: WWW Companion '25; Sydney<sup class="ltx_note_mark">†</sup><sup class="ltx_note_mark">†</sup>ccs: Computing methodologies</p></div>
<div class="ltx_para"><p>Body prose stays.</p></div>
<ul class="ltx_itemize" id="I1">
<li class="ltx_item" id="I1.i1" style="list-style-type:none;"><span class="ltx_tag ltx_tag_item">•</span>
<div class="ltx_para"><p>First contribution.</p></div></li>
<li class="ltx_item" id="I1.i2" style="list-style-type:none;"><span class="ltx_tag ltx_tag_item">•</span>
<div class="ltx_para"><p>Second contribution.</p></div></li>
</ul>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    # List items keep their text but lose the redundant glyph (the Markdown marker already says "bullet").
    assert "First contribution." in markdown
    assert "Second contribution." in markdown
    assert "•" not in markdown
    # The ACM metadata run is gone wholesale; real prose survives.
    assert "journalyear" not in markdown
    assert "ccs:" not in markdown
    assert "†" not in markdown
    assert "Body prose stays." in markdown


async def test_to_markdown_raises_on_empty_conversion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body></body></html>")

    converter = ArxivHtmlConverter(client=_client(handler))

    with pytest.raises(ConversionError):
        await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")


# An ar5iv figure whose <img> carries attributes (id/class/width) — pandoc cannot express those in
# plain Markdown image syntax (link_attributes is disabled), so the tag survives as raw HTML with
# its root-relative, version-qualified `/html/<id>vN/...` src. Exactly what real mirrors showed.
FIGURE_HTML = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Figure Paper</h1>
<div class="ltx_para"><p>Intro text.</p></div>
<figure class="ltx_figure" id="S1.F1">
<img src="/html/2412.15605v2/x1.png" id="S1.F1.g1" class="ltx_graphics" width="830" height="461" alt="Refer to caption"/>
<figcaption class="ltx_caption">Figure 1: the architecture.</figcaption>
</figure>
</div>
</body></html>"""

IMAGE_BYTES = b"\x89PNG fake figure bytes"


async def test_to_markdown_downloads_figures_into_assets_and_rewrites_refs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=FIGURE_HTML)
        if str(request.url) == "https://arxiv.org/html/2412.15605v2/x1.png":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(404)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    # The figure bytes ride along in the result, keyed by a Sources-relative path...
    assert result.assets == {"_assets/2412.15605/x1.png": IMAGE_BYTES}
    # ...and the body references that same path as a plain Markdown image (Obsidian-resolvable).
    assert "![](_assets/2412.15605/x1.png)" in result.markdown
    assert "<img" not in result.markdown
    # The caption is ordinary prose and survives untouched.
    assert "Figure 1: the architecture." in result.markdown


async def test_to_markdown_resolves_page_relative_image_srcs_like_a_browser() -> None:
    # Some arXiv HTML papers reference figures page-relative ("<id>vN/images/...") rather than
    # root-relative. Resolution must replace the page URL's last segment (browser semantics) —
    # naive "<page>/<src>" concatenation doubles the id and 404s.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Relative Paper</h1>
<figure class="ltx_figure"><img src="2603.23512v1/images/dynamics.png" alt="dynamics"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2603.23512":
            return httpx.Response(200, text=page)
        if str(request.url) == "https://arxiv.org/html/2603.23512v1/images/dynamics.png":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(404)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2603.23512")

    # The asset name drops the version-dir prefix, keeping the in-paper path.
    assert result.assets == {"_assets/2603.23512/images-dynamics.png": IMAGE_BYTES}
    assert "![](_assets/2603.23512/images-dynamics.png)" in result.markdown


async def test_to_markdown_retries_bare_relative_srcs_directory_style() -> None:
    # The other relative shape in the wild: a bare "x9.png" src. Browser-style resolution replaces
    # the page URL's last segment (".../html/x9.png" — 404 on arXiv); the directory-style URL
    # ".../html/<id>/x9.png" is what arXiv actually serves, so it's the retry.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Bare Relative Paper</h1>
<figure class="ltx_figure"><img src="x9.png" alt="x9"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2212.10509":
            return httpx.Response(200, text=page)
        if str(request.url) == "https://arxiv.org/html/2212.10509/x9.png":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(404)  # browser-style "https://arxiv.org/html/x9.png" included

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2212.10509")

    assert result.assets == {"_assets/2212.10509/x9.png": IMAGE_BYTES}
    assert "![](_assets/2212.10509/x9.png)" in result.markdown


SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'


def _data_uri_html(payload: str) -> str:
    """A fixture page whose only figure is an inline `data:` URI image (LaTeXML's TikZ rendering)."""
    return f"""<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Diagram Paper</h1>
<div class="ltx_para"><p>Intro text.</p></div>
<figure class="ltx_figure" id="S3.F1">
<img src="data:image/svg+xml;base64,{payload}" id="S3.F1.pic1" class="ltx_picture"/>
<figcaption class="ltx_caption">Figure 1: the pipeline.</figcaption>
</figure>
</div>
</body></html>"""


async def test_to_markdown_decodes_inline_data_uri_images_into_assets() -> None:
    import base64
    import hashlib

    payload = base64.b64encode(SVG_BYTES).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=_data_uri_html(payload))
        return pytest.fail("an inline data: URI must never trigger a fetch")

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    digest = hashlib.sha256(SVG_BYTES).hexdigest()[:8]
    assert result.assets == {f"_assets/2412.15605/inline-{digest}.svg": SVG_BYTES}
    assert f"![](_assets/2412.15605/inline-{digest}.svg)" in result.markdown
    # The base64 blob is gone from the text layer entirely.
    assert "data:" not in result.markdown
    assert "<img" not in result.markdown


async def test_to_markdown_repairs_latexml_inline_svg_for_standalone_rendering() -> None:
    import base64

    # The shape LaTeXML emits for TikZ pictures, as captured from real mirrors: namespace-less root
    # (implied inline in HTML), HTML-lowercased names (viewbox/foreignobject), label text inside a
    # foreignObject whose placement needs SVG2 transform support plus ar5iv CSS variables — viewers
    # without either stack every label at the origin. And — because the math stash ran over the whole
    # page before the diagram was base64-wrapped — a literal math sentinel where a label's formula was.
    latexml_svg = (
        '<svg class="ltx_picture" height="10" id="S3.F1.pic1" overflow="visible" version="1.1" viewbox="0 0 10 10" width="10">'
        '<g fill="#FFFFFF" transform="matrix(1.0 0.0 0.0 1.0 12.2 -20.84)">'
        '<foreignobject height="6.24" overflow="visible" style="--fo_width :1em;--fo_height:0.57em;--fo_depth :0em;"'
        ' transform="matrix(1 0 0 -1 0 6.24)" width="11.03">'
        '<span class="ltx_foreignobject_container"><span class="ltx_text" style="font-size:70%;">'
        "Let \x00MATH0\x00 hold</span></span></foreignobject></g></svg>"
    )
    payload = base64.b64encode(latexml_svg.encode()).decode()
    page = f"""<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Diagram Paper</h1>
<div class="ltx_para"><p>Let <math alttext="x^2" class="ltx_Math" display="inline"><semantics><mrow><msup><mi>x</mi><mn>2</mn></msup></mrow></semantics></math> be a value.</p></div>
<figure class="ltx_figure" id="S3.F1">
<img src="data:image/svg+xml;base64,{payload}" id="S3.F1.pic1" class="ltx_picture"/>
<figcaption class="ltx_caption">Figure 1: the pipeline.</figcaption>
</figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=page)
        return pytest.fail("an inline data: URI must never trigger a fetch")

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    [(key, data)] = result.assets.items()
    assert key.startswith("_assets/2412.15605/inline-")
    assert key.endswith(".svg")
    text = data.decode("utf-8")
    # Standalone-XML repairs: namespace on the root, case-sensitive names restored.
    assert 'xmlns="http://www.w3.org/2000/svg"' in text
    assert "viewBox=" in text
    assert "viewbox=" not in text
    # The label is native SVG text now — no XHTML-in-SVG, no SVG2 dependency, no ar5iv CSS variables.
    assert "foreignobject" not in text.lower()
    assert "<span" not in text
    assert "<text " in text
    # Placement carries over: the flip-back transform verbatim, LaTeX box geometry for the baseline
    # (depth 0em puts the baseline on the box's bottom edge) and the 70% font of an 0.57em-per-6.24-unit box.
    assert 'transform="matrix(1 0 0 -1 0 6.24)"' in text
    assert 'y="6.24"' in text
    assert 'font-size="7.66"' in text
    # The sentinel that rode into the payload opaquely is restored as readable math text.
    assert "\x00" not in text
    assert "Let $x^2$ hold" in text
    # The page's own prose math is untouched by any of this.
    assert "$x^2$ be a value" in result.markdown


async def test_to_markdown_makes_inline_svgs_xml_wellformed_and_scalable() -> None:
    import base64
    import xml.etree.ElementTree as ET

    # Real-mirror defects beyond namespacing: multi-line labels (HTML <br>, illegal as-is in XML),
    # ampersands in label text, and a missing viewBox (no scaling). The label must come out as
    # XML-parseable native text with one tspan per extra line.
    latexml_svg = (
        '<svg class="ltx_picture" height="264.28" id="S1.F1.pic1" overflow="visible" version="1.1" width="347.06">'
        '<g transform="translate(0,264.28)"><foreignobject height="8" overflow="visible" width="8">'
        '<span class="ltx_text">first line<br>second &amp; final</span></foreignobject></g></svg>'
    )
    payload = base64.b64encode(latexml_svg.encode()).decode()
    page = f"""<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Wellformed Paper</h1>
<figure class="ltx_figure"><img src="data:image/svg+xml;base64,{payload}" class="ltx_picture"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    [(_, data)] = result.assets.items()
    text = data.decode("utf-8")
    parsed = ET.fromstring(text)  # noqa: S314 — fixture-produced bytes; asserting well-formedness, not parsing untrusted input
    assert 'viewBox="0 0 347.06 264.28"' in text
    svg_ns = "{http://www.w3.org/2000/svg}"
    [text_element] = parsed.iter(f"{svg_ns}text")
    [tspan] = text_element.iter(f"{svg_ns}tspan")
    assert text_element.text == "first line"
    assert tspan.text == "second & final"


async def test_to_markdown_strips_leaked_description_macro() -> None:
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Macro Paper</h1>
<figure class="ltx_figure"><div class="ltx_para"><p>\\Description</p></div>
<div class="ltx_para"><p>This figure compares workflows.</p></div>
<figcaption>Figure 1: comparison.</figcaption></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert "\\Description" not in result.markdown
    assert "This figure compares workflows." in result.markdown


async def test_to_markdown_leaves_an_undecodable_data_uri_reference_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=_data_uri_html("@@not-base64@@"))
        return pytest.fail("an inline data: URI must never trigger a fetch")

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    # Verbatim preservation beats a garbage rewrite: the reference stays, nothing is captured.
    assert result.assets == {}
    assert "data:image/svg+xml;base64,@@not-base64@@" in result.markdown


async def test_to_markdown_keeps_remote_image_url_when_figure_fetch_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=FIGURE_HTML)
        return httpx.Response(404)  # every image request fails

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    # No asset captured, but the reference degrades to the absolute arXiv URL instead of a dead
    # root-relative path — the figure still renders for online readers and the run never fails.
    assert result.assets == {}
    assert "![](https://arxiv.org/html/2412.15605v2/x1.png)" in result.markdown
    assert "<img" not in result.markdown


async def test_to_markdown_skips_off_host_absolute_image_src_without_fetching() -> None:
    # Mild SSRF guard: an absolute src pointing off arxiv.org (here a look-alike "evilarxiv.org"
    # plus a foreign host) must never be fetched. The figure degrades exactly like a failed
    # download — no asset, the reference kept as the (disallowed) remote URL — and no request
    # for the off-host URL ever reaches the transport.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Off-Host Paper</h1>
<figure class="ltx_figure"><img src="http://evil.example/x.png" id="S1.F1.g1" class="ltx_graphics" alt="x"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=page)
        return pytest.fail(f"an off-host image src must never be fetched, got {request.url}")

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    # Nothing captured; the reference degrades to the off-host URL (kept verbatim, never fetched).
    assert result.assets == {}
    assert "![](http://evil.example/x.png)" in result.markdown
    assert "<img" not in result.markdown


async def test_to_markdown_skips_arxiv_look_alike_host_image_src() -> None:
    # The allowlist suffix-matches ".arxiv.org" / exact "arxiv.org" — a naive "arxiv.org in host"
    # check would wrongly admit "evilarxiv.org", so guard that shape explicitly.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Look-Alike Paper</h1>
<figure class="ltx_figure"><img src="https://evilarxiv.org/x.png" id="S1.F1.g1" class="ltx_graphics" alt="x"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=page)
        return pytest.fail(f"an arxiv.org look-alike host must never be fetched, got {request.url}")

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert result.assets == {}
    assert "![](https://evilarxiv.org/x.png)" in result.markdown


async def test_to_markdown_fetches_static_arxiv_subdomain_image_src() -> None:
    # The allowlist admits arxiv.org subdomains (e.g. static.arxiv.org) over https, so a normal
    # cross-subdomain figure still downloads and localises like any other.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Subdomain Paper</h1>
<figure class="ltx_figure"><img src="https://static.arxiv.org/x1.png" id="S1.F1.g1" class="ltx_graphics" alt="x"/></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://arxiv.org/html/2412.15605":
            return httpx.Response(200, text=page)
        if str(request.url) == "https://static.arxiv.org/x1.png":
            return httpx.Response(200, content=IMAGE_BYTES)
        return httpx.Response(404)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    assert result.assets == {"_assets/2412.15605/x1.png": IMAGE_BYTES}
    assert "![](_assets/2412.15605/x1.png)" in result.markdown


async def test_to_markdown_converts_latexml_tables_to_pipe_markdown() -> None:
    # The real ar5iv table shape: wrapped in <figure> (which forces pandoc's raw-HTML fallback
    # for the whole block unless handled), colspan group headers, a rowspan, a nested single-column
    # ltx_tabular standing in for a multi-line cell label, and a full-width section-divider row.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Table Paper</h1>
<figure id="S4.T3" class="ltx_table">
<table id="S4.T3.60" class="ltx_tabular ltx_guessed_headers">
<tbody>
<tr><td class="ltx_td"></td><td colspan="2" class="ltx_td ltx_align_center">Simple QA</td></tr>
<tr><td class="ltx_td ltx_th_row">Retrieval</td><td>NQ</td><td>PopQA</td></tr>
<tr><td colspan="3" class="ltx_td ltx_align_center">Simple Baselines</td></tr>
<tr><td rowspan="2" class="ltx_td">BM25</td><td>56.1</td><td>35.7</td></tr>
<tr><td>57.0</td><td>36.0</td></tr>
<tr><td><table class="ltx_tabular"><tbody><tr><td>HippoRAG</td></tr><tr><td>(ColBERTv2)</td></tr></tbody></table></td><td>55.0</td><td>40.1</td></tr>
<tr><td>Cases</td><td>1. I. P. Paul<br class="ltx_break"/>2. Yinka Ayefele</td><td>n/a</td></tr>
</tbody>
</table>
<figcaption>Table 3: retrieval recall.</figcaption>
</figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    # No raw HTML survives — the whole figure becomes Markdown.
    assert "<table" not in markdown
    assert "<figure" not in markdown
    assert "<td" not in markdown
    assert "<figcaption" not in markdown
    # Spans expand into aligned pipe rows: colspan pads its own row, rowspan pads the row below.
    # (pandoc pads cells for column alignment, so rows are compared whitespace-normalised.)
    rows = [" ".join(line.split()) for line in markdown.splitlines()]
    assert "| Retrieval | NQ | PopQA |" in rows
    assert "| BM25 | 56.1 | 35.7 |" in rows
    assert "| | 57.0 | 36.0 |" in rows
    assert "| Simple Baselines | | |" in rows
    # The nested multi-line-label table flattens into its cell, joined with spaces.
    assert "| HippoRAG (ColBERTv2) | 55.0 | 40.1 |" in rows
    # In-cell line breaks flatten to spaces — a single LineBreak in any cell would force the WHOLE
    # table back to raw HTML (pipe cells are single-line by definition). pandoc escapes the
    # cell-leading "1." so it can't parse as a list ("1\." renders as "1.").
    assert "| Cases | 1\\. I. P. Paul 2. Yinka Ayefele | n/a |" in rows
    # The caption is an ordinary paragraph after the table.
    assert "Table 3: retrieval recall." in markdown


async def test_to_markdown_flattens_tables_nested_inside_header_cells() -> None:
    # The nested-table layout hack also occurs inside <th> cells (real HippoRAG appendix shape).
    # Missing it is sticky: the inner table would be normalised standalone, and the outer table —
    # forever containing a <table> — could never match the innermost-table pass again, falling back
    # to raw HTML wholesale.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Header Cell Paper</h1>
<table class="ltx_tabular">
<thead>
<tr><th class="ltx_th"><table class="ltx_tabular"><tbody><tr><td>Question</td></tr><tr><td>Decomposition</td></tr></tbody></table></th><th class="ltx_th">Answer</th></tr>
</thead>
<tbody>
<tr><td>Who navigated first?</td><td>Magellan</td></tr>
</tbody>
</table>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "<table" not in markdown
    rows = [" ".join(line.split()) for line in markdown.splitlines()]
    assert "| Question Decomposition | Answer |" in rows
    assert "| Who navigated first? | Magellan |" in rows


async def test_to_markdown_unwraps_single_column_prompt_tables_with_lists() -> None:
    # The LeanRAG appendix shape: a one-column, two-row prompt box whose body cell holds a <ul>.
    # As a table, the block content forces pandoc's raw-HTML fallback; as prose it's a heading
    # paragraph plus a list — so single-column tables unwrap row-by-row with content kept raw.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Prompt List Paper</h1>
<table class="ltx_tabular"><tbody>
<tr><td>Entity aggregation prompt</td></tr>
<tr><td><div class="ltx_para"><p>Role: Analyst</p></div>
<ul class="ltx_itemize"><li class="ltx_item"><div class="ltx_para"><p>author: LangGPT</p></div></li>
<li class="ltx_item"><div class="ltx_para"><p>version: 1.1</p></div></li></ul></td></tr>
</tbody></table>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "<table" not in markdown
    assert "|" not in markdown
    assert "Entity aggregation prompt" in markdown
    assert "Role: Analyst" in markdown
    # The list survives as a Markdown list (pandoc writes it loose — items hold ltx_para
    # paragraphs), not as a flattened blob.
    assert markdown.count("\n-") >= 2
    assert "author: LangGPT" in markdown
    assert "version: 1.1" in markdown


async def test_to_markdown_unwraps_single_cell_prompt_tables() -> None:
    # LaTeXML renders prompt/listing boxes as a 1×1 ltx_tabular. As a table that's a one-cell pipe
    # monster; as prose it's just paragraphs — so it unwraps, keeping the paragraphs.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Prompt Paper</h1>
<figure class="ltx_table">
<table class="ltx_tabular"><tbody><tr><td class="ltx_td">
<div class="ltx_para"><p>You are a helpful assistant.</p></div>
<div class="ltx_para"><p>Extract all entities from the passage.</p></div>
</td></tr></tbody></table>
<figcaption>Table 7: the NER prompt.</figcaption>
</figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "<table" not in markdown
    assert "|" not in markdown  # no degenerate one-cell pipe table either
    assert "You are a helpful assistant." in markdown
    assert "Extract all entities from the passage." in markdown
    assert "Table 7: the NER prompt." in markdown


async def test_to_markdown_unwraps_numeric_math_typography() -> None:
    # ar5iv wraps every table/prose number in <math>; \mathbf marks best results, \underline
    # second-best. Numbers must come out as plain text (bold/italic for the marks) while genuine
    # mathematics keeps its $...$.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Numbers Paper</h1>
<div class="ltx_para"><p>Plain <math alttext="48.6"><mn>48.6</mn></math>,
best <math alttext="\\mathbf{71.0}"><mn>71.0</mn></math>,
runner-up <math alttext="\\underline{93.9}"><mn>93.9</mn></math>,
share <math alttext="95\\%"><mn>95</mn></math>,
significance <math alttext="p&lt;0.05"><mi>p</mi></math> and index <math alttext="F_1"><mi>F</mi></math>.</p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "Plain 48.6," in markdown
    assert "best **71.0**," in markdown
    assert "runner-up *93.9*," in markdown
    assert "share 95%," in markdown
    # Real math is untouched.
    assert "$p<0.05$" in markdown
    assert "$F_1$" in markdown
    assert "$48.6$" not in markdown


async def test_to_markdown_escapes_math_pipes_inside_table_cells() -> None:
    # A '|' inside restored math would split the pipe-table cell. Inside a table line it must be
    # escaped; the same math in prose keeps the literal pipe.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Pipe Paper</h1>
<div class="ltx_para"><p>In prose: <math alttext="P(a|b)"><mi>P</mi></math>.</p></div>
<table class="ltx_tabular"><tbody>
<tr><td>Metric</td><td>Formula</td></tr>
<tr><td>Conditional</td><td><math alttext="P(a|b)"><mi>P</mi></math></td></tr>
</tbody></table>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "In prose: $P(a|b)$." in markdown
    rows = [" ".join(line.split()) for line in markdown.splitlines()]
    assert "| Conditional | $P(a\\|b)$ |" in rows


async def test_to_markdown_replaces_pgf_blobs_in_math_with_their_labels() -> None:
    # ComoRAG's Algorithm 1 draws TikZ operator boxes *inside* math, so the alttext carries raw
    # multi-line \pgfpicture source — unrenderable by any Markdown math engine. The blob becomes
    # its drawn label as \text{...} and the restored $...$ span stays single-line.
    blob = (
        "\\raisebox{0.73193pt}{ \\hbox to66.6pt{\\vbox to12.14pt{\\pgfpicture\\makeatletter\\hbox{\\hskip 33.29pt"
        "\n{{}}\\hbox{\\hbox{{\\pgfsys@beginscope\\pgfsys@invoke{ }\\definecolor[named]{pgfstrokecolor}{rgb}{0,0,0}"
        "\\pgfsys@color@gray@fill{0}Tri-Retrieve}}}} }}\\pgfsys@invoke{ }\\pgfsys@endscope\\hss}\\endpgfpicture}}}"
    )
    alttext = f"\\mathcal{{E}}^{{(0)}}\\leftarrow\\,{blob}\\,(\\{{q_{{init}}\\}},\\mathcal{{X}})"
    page = f"""<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Algorithm Paper</h1>
<div class="ltx_para"><p>5: <math alttext="{alttext}" class="ltx_Math" display="inline"><semantics><mrow><mi>E</mi></mrow></semantics></math></p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.10419")
    markdown = result.markdown

    assert "pgfpicture" not in markdown
    assert "pgfsys" not in markdown
    assert "$\\mathcal{E}^{(0)}\\leftarrow\\,\\text{Tri-Retrieve}\\,(\\{q_{init}\\},\\mathcal{X})$" in markdown


async def test_to_markdown_restores_mangled_backtick_fences_in_verbatim() -> None:
    # LaTeX verbatim renders ` as a typographic left quote, so a prompt that contains a Markdown
    # fence arrives as ‘‘‘json. Restored to real backticks — and the enclosing fence must grow to
    # four backticks or the inner fence would terminate the block early. Fence attributes
    # ({.ltx_verbatim ...}) must not appear.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Verbatim Paper</h1>
<pre class="ltx_verbatim ltx_font_typewriter">‘‘‘json
{"probe1": "Content of probe 1"}
‘‘‘</pre>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.10419")
    markdown = result.markdown

    assert "{.ltx_verbatim" not in markdown
    assert "‘" not in markdown
    assert "````\n```json" in markdown
    assert '{"probe1": "Content of probe 1"}' in markdown
    assert "```\n````" in markdown


async def test_to_markdown_breaks_paragraph_spans_in_unwrapped_boxes() -> None:
    # Case-study boxes are single-column tables whose "paragraphs" are LaTeXML ltx_p *spans* —
    # inline elements. pandoc's HTML reader merges inline runs across blank lines, so without real
    # block boundaries the whole unwrapped box becomes one wall of text. Real shape from ComoRAG
    # A5.T8: one span per row, plus one row with two spans.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Case Study Paper</h1>
<table class="ltx_tabular"><tbody>
<tr><td><span class="ltx_text ltx_font_bold">Input Data (No Options) Query: why buy ink?</span></td></tr>
<tr><td><span class="ltx_inline-block">
<span class="ltx_p" style="width:495.8pt;">Options: [A] letters [B] photos</span>
<span class="ltx_p" style="width:495.8pt;">Chosen: C. (Correct)</span>
</span></td></tr>
</tbody></table>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.10419")
    lines = [line.strip() for line in result.markdown.splitlines()]

    [query_line] = [line for line in lines if "why buy ink?" in line]
    [options_line] = [line for line in lines if "Options:" in line]
    assert "Options:" not in query_line  # separate rows are separate lines
    assert "Chosen:" not in options_line  # two spans within one row break too


async def test_to_markdown_sanitises_unrenderable_latex_in_math() -> None:
    # alttext is raw author LaTeX. Three real-mirror shapes that break MathJax/KaTeX: \mathdollar
    # (kernel macro -> renders as literal text), % line-continuations (comment out the formula's
    # remainder), and \leavevmode\nobreak spacing internals. Currency stays math ($\$50K$) so the
    # bare $ can't pair up with a later one and re-trigger math parsing.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Money Paper</h1>
<div class="ltx_para"><p>Annotators exceeded <math alttext="\\mathdollar 200"><mn>200</mn></math> in pay.</p></div>
<div class="ltx_para"><p>Set <math alttext="\\mathcal{M}=\\Big{(}\\mathcal{G},\\leavevmode\\nobreak\\ \\mathcal{D}%
\\big{)}"><mi>M</mi></math> holds.</p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.20867")
    markdown = result.markdown

    assert "exceeded $\\$200$ in pay." in markdown
    assert "mathdollar" not in markdown
    # Continuation joined, spacing no-ops gone, math single-line.
    assert "$\\mathcal{M}=\\Big{(}\\mathcal{G},\\ \\mathcal{D}\\big{)}$" in markdown
    assert "leavevmode" not in markdown
    assert "%" not in markdown


async def test_to_markdown_strips_leaked_author_note_macros() -> None:
    # AAAI/ACM class macros the authors' \author block leaves unexpanded leak as literal text
    # ("Juyuan Wang1\equalcontrib Rongchen Zhao1\equalcontrib ...").
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Authors Paper</h1>
<div class="ltx_para"><p>Juyuan Wang<sup>1</sup>\\equalcontrib Rongchen Zhao<sup>1</sup>\\equalcontrib Wei Wei<sup>2</sup></p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.10419")

    assert "equalcontrib" not in result.markdown
    assert "Juyuan Wang<sup>1</sup>" in result.markdown


async def test_to_markdown_unindents_accidental_code_blocks() -> None:
    # The ar5iv author block's inter-element whitespace survives pandoc as 4-5 leading spaces —
    # an *indented code block* in Markdown, so reading mode shows the raw <sup> tags in monospace.
    # Legitimate indentation must survive: nested list items and fenced-code bodies.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Indent Paper</h1>
<div class="ltx_authors"><p>
     Weitao Li<sup>1,2</sup>, Ante Wang<sup>2</sup></p></div>
<ul class="ltx_itemize"><li class="ltx_item"><div class="ltx_para"><p>outer item</p></div>
<ul class="ltx_itemize"><li class="ltx_item"><div class="ltx_para"><p>nested item</p></div></li></ul></li></ul>
<pre class="ltx_verbatim">{
    "indented": "stays"
}</pre>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.06165")
    markdown = result.markdown

    [author_line] = [line for line in markdown.splitlines() if "Weitao Li" in line]
    assert not author_line.startswith(" ")  # no accidental code block
    [json_line] = [line for line in markdown.splitlines() if '"indented"' in line]
    assert json_line.startswith("    ")  # fenced body keeps its indentation
    [nested_line] = [line for line in markdown.splitlines() if "nested item" in line]
    assert nested_line.lstrip() != nested_line  # nested list keeps its indentation


async def test_to_markdown_strips_duplicated_author_footnote_marks() -> None:
    # ar5iv glues author footnotes into the author line as duplicated <sup> marks plus a literal
    # footnotetext:/thanks: keyword (or the mark repeated as a digit). Marks and keyword go; the
    # note's text stays; a single <sup> affiliation marker is untouched.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Notes Paper</h1>
<div class="ltx_para"><p>Yang Liu<sup>1,2</sup><sup>†</sup><sup>†</sup>footnotetext: <math alttext="\\dagger"><mo>†</mo></math> Corresponding authors.</p></div>
<div class="ltx_para"><p>Yuzheng Cai<sup>†</sup><span class="ltx_note"><sup>†</sup>thanks: Equal contribution.</span>, Zhenyue Guo</p></div>
<div class="ltx_para"><p>Haoyu Huang<sup>1</sup><span class="ltx_note"><sup>1</sup>1The Ohio State University</span></p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.06165")
    markdown = result.markdown

    assert "footnotetext" not in markdown
    assert "thanks:" not in markdown
    assert "Yang Liu<sup>1,2</sup>" in markdown  # real affiliation marker survives the merged form
    assert "$\\dagger$ Corresponding authors." in markdown
    assert "Yuzheng Cai Equal contribution., Zhenyue Guo" in markdown
    assert "Haoyu Huang The Ohio State University" in markdown


async def test_to_markdown_emits_hard_breaks_as_trailing_spaces() -> None:
    # Author/affiliation blocks separate lines with hard breaks. pandoc's default backslash-newline
    # form renders as a literal backslash or a stray gap in several editors; the trailing-two-spaces
    # form renders everywhere.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Affiliation Paper</h1>
<div class="ltx_para"><p>Weitao Li<sup>1,2</sup>, Ante Wang<sup>2</sup>,<br/>
<sup>1</sup> Dept. of Comp. Sci., Tsinghua University, Beijing, China<br/>
<sup>2</sup> Institute for AI Industry Research (AIR)</p></div>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2508.06165")

    assert "\\\n" not in result.markdown
    assert "Ante Wang<sup>2</sup>,  \n<sup>1</sup> Dept." in result.markdown


async def test_to_markdown_folds_inline_decoration_svgs_into_prose() -> None:
    import base64

    # Authors draw TikZ boxes around operation names mid-sentence ("the [Self-Probe] operation");
    # LaTeXML emits them as tiny inline SVG pictures. As extracted image files they read as broken
    # figures mid-prose — the content IS the label text, so it folds back in as bold text. A diagram
    # SVG standing on its own line keeps the asset treatment.
    decoration = (
        '<svg class="ltx_picture" height="11" width="62"><g>'
        '<foreignobject height="9" width="60"><span class="ltx_text">Self-Probe</span></foreignobject></g></svg>'
    )
    diagram = (
        '<svg class="ltx_picture" height="120" width="200"><g>'
        '<foreignobject height="9" width="80"><span class="ltx_text">Memory Pool</span></foreignobject></g></svg>'
    )
    decoration_payload = base64.b64encode(decoration.encode()).decode()
    diagram_payload = base64.b64encode(diagram.encode()).decode()
    page = f"""<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Decoration Paper</h1>
<div class="ltx_para"><p>invoked as a <img src="data:image/svg+xml;base64,{decoration_payload}" class="ltx_picture"/> operation here.</p></div>
<figure class="ltx_figure"><img src="data:image/svg+xml;base64,{diagram_payload}" class="ltx_picture"/>
<figcaption>Figure 2: the memory pool.</figcaption></figure>
</div>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    # The decoration is prose again; no asset, no image reference mid-sentence.
    assert "invoked as a **Self-Probe** operation here." in markdown
    # The standalone diagram still becomes an asset (exactly one).
    [(key, _)] = result.assets.items()
    assert key.startswith("_assets/2412.15605/inline-")
    assert f"![]({key})" in markdown
    assert "Figure 2: the memory pool." in markdown


async def test_to_markdown_extracts_text_box_svgs_as_blockquotes() -> None:
    import base64

    # TikZ prompt/algorithm boxes are pictures whose entire content is paragraph text: a couple of
    # background <path>s plus foreignObjects holding ltx_p paragraphs. Native SVG <text> cannot wrap,
    # so no asset repair can render a paragraph inside the box — and as an image the text is invisible
    # to retrieval. The box folds back into the document as a blockquote: a label-sized first block
    # becomes the bold title, ltx_p paragraphs become hard-break lines, Markdown-active characters
    # (the literal <|special_tokens|> prompts are full of) are escaped.
    prompt_box = (
        '<svg class="ltx_picture" height="200" width="600">'
        '<g><g fill="#999999"><path d="M 0 0 L 600 200 Z"/></g>'
        '<g fill="#F9F9F9"><path d="M 0 0 L 600 190 Z"/></g>'
        '<g><foreignobject height="9" width="560"><span class="ltx_foreignobject_container">'
        '<span class="ltx_p">Prompt for MMLU-Pro</span></span></foreignobject></g>'
        '<g><foreignobject height="180" width="560"><span class="ltx_foreignobject_container">'
        '<span id="A4.p1.pic1.1.1.p1" class="ltx_p">Instruction: analyze every option carefully and issue a search query like '
        "&lt;|begin_of_query|&gt; your query &lt;|end_of_query|&gt; at any reasoning step.</span>"
        '<span id="A4.p1.pic1.1.1.p2" class="ltx_p">Final answer format: the correct answer is: A, B, C, D.</span>'
        "</span></foreignobject></g></g></svg>"
    )
    payload = base64.b64encode(prompt_box.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_data_uri_html(payload))

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert result.assets == {}
    assert "![](" not in markdown
    expected = (
        "> **Prompt for MMLU-Pro**\n"
        ">\n"
        "> Instruction: analyze every option carefully and issue a search query like "
        "\\<|begin_of_query|\\> your query \\<|end_of_query|\\> at any reasoning step.  \n"
        "> Final answer format: the correct answer is: A, B, C, D."
    )
    assert expected in markdown


async def test_to_markdown_keeps_diagrams_with_drawing_primitives_as_assets() -> None:
    import base64

    # Long text alone must not reclassify a genuine diagram: drawing primitives (lines, circles,
    # rects...) mean the picture carries information the text layer can't, so it stays an asset.
    diagram = (
        '<svg class="ltx_picture" height="120" width="400"><g>'
        '<line x1="0" y1="0" x2="100" y2="100"/>'
        '<foreignobject height="9" width="380"><span class="ltx_text">'
        "This architecture connects the retriever to the reader through a shared memory pool "
        "updated after every reasoning step.</span></foreignobject></g></svg>"
    )
    payload = base64.b64encode(diagram.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_data_uri_html(payload))

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    [(key, _)] = result.assets.items()
    assert key.startswith("_assets/2412.15605/inline-")
    assert f"![]({key})" in result.markdown


async def test_to_markdown_wraps_paragraph_length_diagram_labels_into_tspans() -> None:
    import base64
    import xml.etree.ElementTree as ET

    # A paragraph-length foreignObject inside a *diagram* (drawing primitives keep it an asset)
    # cannot stay a single native-text line — SVG text never wraps, so it would overflow the box
    # by thousands of units. It is word-wrapped into tspans within the box-width character budget.
    long_text = (
        "You are a helpful assistant that answers multi-hop questions by decomposing them into "
        "single-hop queries and verifying every intermediate answer against retrieved evidence."
    )
    diagram = (
        '<svg class="ltx_picture" height="120" width="400"><g>'
        '<circle cx="5" cy="5" r="2"/>'
        '<foreignobject height="6.24" width="220" style="--fo_height:0.57em;--fo_depth:0em;"'
        f' transform="matrix(1 0 0 -1 0 6.24)"><span class="ltx_text">{long_text}</span>'
        "</foreignobject></g></svg>"
    )
    payload = base64.b64encode(diagram.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_data_uri_html(payload))

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    [(_, data)] = result.assets.items()
    parsed = ET.fromstring(data.decode("utf-8"))  # noqa: S314 — fixture-produced bytes; asserting well-formedness
    svg_ns = "{http://www.w3.org/2000/svg}"
    [text_element] = parsed.iter(f"{svg_ns}text")
    # em = 6.24/0.57 ≈ 10.95 → budget = 220 / (10.95 × 0.55) ≈ 36 chars per line.
    lines = [text_element.text or "", *(tspan.text or "" for tspan in text_element.iter(f"{svg_ns}tspan"))]
    assert len(lines) >= 4
    assert all(len(line) <= 36 for line in lines)
    assert " ".join(lines) == long_text


async def test_to_markdown_reads_modern_ltx_fo_geometry_css_vars() -> None:
    import base64

    # arxiv.org/html (current LaTeXML) writes --ltx-fo-height/--ltx-fo-depth where older ar5iv
    # snapshots wrote --fo_height/--fo_depth. Both forms must drive the baseline/em geometry —
    # on the fallback path every label gets font-size 10 at 0.8×height, visibly wrong.
    latexml_svg = (
        '<svg class="ltx_picture" height="10" id="S3.F1.pic1" overflow="visible" version="1.1" viewbox="0 0 10 10" width="10">'
        '<g fill="#FFFFFF" transform="matrix(1.0 0.0 0.0 1.0 12.2 -20.84)">'
        '<foreignobject height="6.24" overflow="visible" style="--ltx-fo-width:1em;--ltx-fo-height:0.57em;--ltx-fo-depth:0em;"'
        ' transform="matrix(1 0 0 -1 0 6.24)" width="11.03">'
        '<span class="ltx_foreignobject_container"><span class="ltx_text" style="font-size:70%;">'
        "tiny label</span></span></foreignobject></g></svg>"
    )
    payload = base64.b64encode(latexml_svg.encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_data_uri_html(payload))

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")

    [(_, data)] = result.assets.items()
    text = data.decode("utf-8")
    # Same geometry as the legacy-var test: depth 0 puts the baseline on the box edge,
    # 0.57em over 6.24 units gives 10.95/em, ×70% font = 7.66.
    assert 'y="6.24"' in text
    assert 'font-size="7.66"' in text


async def test_to_markdown_strips_latexml_footer_and_keeps_superscripts_portable() -> None:
    # The ar5iv <footer> carries "Generated on ... by LaTeXML" plus the LaTeXML logo — page chrome
    # that must not reach the mirror as a junk line or a junk logo asset. Footnote marks must come
    # out as <sup>1</sup> (renders on GitHub and Obsidian alike), never as pandoc-only ^1^.
    page = """<!DOCTYPE html>
<html><body>
<div class="ltx_page_main">
<h1 class="ltx_title ltx_title_document">Chrome Paper</h1>
<div class="ltx_para"><p>Result is significant<sup class="ltx_note_mark">1</sup> here.</p></div>
</div>
<footer class="ltx_page_footer">
<div class="ltx_page_logo">Generated on Tue Jan 14 16:17:22 2025 by <a href="http://dlmf.nist.gov/LaTeXML/"><img src="data:image/png;base64,iVBORw0KGgo=" alt="LaTeXML"/>LaTeXML</a></div>
</footer>
</body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    converter = ArxivHtmlConverter(client=_client(handler))

    result = await converter.to_markdown(PDF_PATH, source_url="https://arxiv.org/abs/2412.15605")
    markdown = result.markdown

    assert "Generated on" not in markdown
    assert "LaTeXML" not in markdown
    assert result.assets == {}  # the logo never becomes an asset
    assert "significant<sup>1</sup> here" in markdown
    assert "^1^" not in markdown
