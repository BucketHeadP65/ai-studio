"""Unit tests for `AzureDocumentConverter` with a fully faked Azure aio client.

No real network or Azure call happens and nothing is monkeypatched: the converter
takes an injectable `client_factory`, so each test passes a factory returning an
async-context-manager fake client whose poller's awaitable `result()` yields a stub
`AnalyzeResult`. The fake also records the call arguments so tests can assert that
`prebuilt-layout` + Markdown + the FORMULAS add-on were requested.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import pytest

from azure.ai.documentintelligence.models import DocumentAnalysisFeature, DocumentContentFormat, DocumentPage
from azure.core.exceptions import AzureError, ClientAuthenticationError, HttpResponseError, ServiceRequestError
from core.settings import DocumentIntelligenceSettings
from engram.sources.contracts import ConversionError, ConversionResult
from pydantic import ValidationError

from engram_connectors.conversion.contracts import AzureDocumentConverter
from engram_connectors.conversion.infrastructure.azure_converter import AzureAnalyzeResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import Path

ENDPOINT = "https://fake.cognitiveservices.azure.com/"
LATEX_MARKDOWN = "# Paper\n$E=mc^2$"
_PAGE = DocumentPage(page_number=1, spans=[])


@dataclass
class FakeResult:
    """Stub `AnalyzeResult` — the two fields the adapter reads."""

    content: str | None
    pages: Sequence[DocumentPage] | None


@dataclass
class FakeRenamedResult:
    """Stub `AnalyzeResult` after an upstream rename — `content` is gone (renamed to `markdown`).

    Stands in for an Azure-DI shape drift: the adapter must surface this as a clean
    `ValidationError` at the boundary, not an `AttributeError` (or silently empty output).
    """

    markdown: str | None
    pages: Sequence[DocumentPage] | None


class FakePoller:
    """Stand-in for the analyze poller whose awaitable `result()` yields a stub."""

    def __init__(self, result: FakeResult) -> None:
        self._result = result

    async def result(self) -> FakeResult:
        return self._result


class FakeDocumentIntelligenceClient:
    """Async-context-manager stand-in for the aio `DocumentIntelligenceClient`.

    Carries the scripted `result` its poller returns, and records the positional
    `model_id` and the `body`/`output_content_format`/`features` kwargs so tests
    can assert what was requested.
    """

    def __init__(self, result: FakeResult) -> None:
        self._result = result
        self.model_id: str | None = None
        self.body: object = None
        self.output_content_format: object = None
        self.features: object = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def begin_analyze_document(self, model_id: str, *, body: object, output_content_format: object, features: object) -> FakePoller:
        self.model_id = model_id
        self.body = body
        self.output_content_format = output_content_format
        self.features = features
        return FakePoller(self._result)


def _converter(client: FakeDocumentIntelligenceClient) -> AzureDocumentConverter:
    """An `AzureDocumentConverter` whose client factory yields the supplied fake client."""

    @asynccontextmanager
    async def factory() -> AsyncGenerator[FakeDocumentIntelligenceClient]:
        yield client

    return AzureDocumentConverter(DocumentIntelligenceSettings(endpoint=ENDPOINT), client_factory=factory)


@pytest.fixture
def tmp_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake bytes")
    return pdf


async def test_to_markdown_returns_content_and_page_count(tmp_pdf: Path) -> None:
    converter = _converter(FakeDocumentIntelligenceClient(FakeResult(content=LATEX_MARKDOWN, pages=[_PAGE])))

    result = await converter.to_markdown(tmp_pdf)

    assert "$E=mc^2$" in result.markdown
    assert result.page_count == 1
    assert result.converter == "azure-di/prebuilt-layout+formulas"


async def test_to_markdown_requests_prebuilt_layout_markdown(tmp_pdf: Path) -> None:
    client = FakeDocumentIntelligenceClient(FakeResult(content="# ok", pages=[_PAGE]))
    converter = _converter(client)

    await converter.to_markdown(tmp_pdf)

    assert client.model_id == "prebuilt-layout"
    assert client.output_content_format == DocumentContentFormat.MARKDOWN
    assert client.features == [DocumentAnalysisFeature.FORMULAS]


async def test_to_markdown_ignores_source_url_hint(tmp_pdf: Path) -> None:
    converter = _converter(FakeDocumentIntelligenceClient(FakeResult(content=LATEX_MARKDOWN, pages=[_PAGE])))

    result = await converter.to_markdown(tmp_pdf, source_url="https://arxiv.org/abs/1234.5678")

    assert "$E=mc^2$" in result.markdown


@pytest.mark.parametrize("empty_content", ["", None])
async def test_to_markdown_raises_on_empty_content(tmp_pdf: Path, empty_content: str | None) -> None:
    converter = _converter(FakeDocumentIntelligenceClient(FakeResult(content=empty_content, pages=[])))

    with pytest.raises(ConversionError):
        await converter.to_markdown(tmp_pdf)


async def test_to_markdown_raises_when_endpoint_unset(tmp_pdf: Path) -> None:
    # `DocumentIntelligenceSettings.endpoint` defaults to "" so the settings tree composes on a
    # bare config; with no injected factory the real Managed-Identity path must surface a clear error
    # (and never reach the Azure SDK / credential) rather than failing obscurely downstream.
    converter = AzureDocumentConverter(DocumentIntelligenceSettings())

    with pytest.raises(ConversionError, match="endpoint"):
        await converter.to_markdown(tmp_pdf)


async def test_to_markdown_raises_validation_error_on_drifted_azure_shape(tmp_pdf: Path) -> None:
    # An Azure-DI shape change (here: `content` renamed away) must surface as a clean
    # `ValidationError` at the boundary — not an AttributeError, and not a silently-empty result.
    converter = _converter(FakeDocumentIntelligenceClient(FakeRenamedResult(markdown="# md", pages=[_PAGE])))  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        await converter.to_markdown(tmp_pdf)


class TestAzureAnalyzeResponse:
    """The frozen boundary model the adapter validates the SDK result through."""

    def test_validates_sdk_result_via_from_attributes(self) -> None:
        response = AzureAnalyzeResponse.model_validate(FakeResult(content=LATEX_MARKDOWN, pages=[_PAGE, _PAGE]), from_attributes=True)

        assert response.content == LATEX_MARKDOWN
        assert response.pages is not None
        assert len(response.pages) == 2

    def test_missing_content_field_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            AzureAnalyzeResponse.model_validate(FakeRenamedResult(markdown="# md", pages=[]), from_attributes=True)

    def test_is_frozen(self) -> None:
        response = AzureAnalyzeResponse.model_validate(FakeResult(content="# md", pages=[]), from_attributes=True)

        with pytest.raises(ValidationError):
            response.content = "mutated"  # type: ignore[misc]


class TestConversionResultFrozen:
    """`ConversionResult` carries the HTTP-boundary convention's frozen config."""

    def test_is_frozen(self) -> None:
        result = ConversionResult(markdown="# md", page_count=1, converter="azure-di/prebuilt-layout+formulas")

        with pytest.raises(ValidationError):
            result.markdown = "mutated"  # type: ignore[misc]


class FakeFailingClient(FakeDocumentIntelligenceClient):
    """A client whose analyze call raises the scripted Azure error."""

    def __init__(self, error: AzureError) -> None:
        super().__init__(FakeResult(content="# never read", pages=[]))
        self.error = error

    async def begin_analyze_document(self, model_id: str, *, body: object, output_content_format: object, features: object) -> FakePoller:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        ServiceRequestError("connection refused"),
        ClientAuthenticationError("no credential"),
        HttpResponseError("the service refused the request"),
    ],
)
async def test_to_markdown_raises_conversion_error_when_azure_fails(tmp_pdf: Path, error: AzureError) -> None:
    converter = _converter(FakeFailingClient(error))

    with pytest.raises(ConversionError, match=r"paper\.pdf"):
        await converter.to_markdown(tmp_pdf)


async def test_to_markdown_refuses_a_missing_pdf_as_a_conversion_error(tmp_path: Path) -> None:
    """A path nothing downloaded is a typed refusal, never a raw FileNotFoundError from `open`."""
    converter = _converter(FakeDocumentIntelligenceClient(FakeResult(content="# ok", pages=[_PAGE])))

    with pytest.raises(ConversionError, match="no PDF on disk"):
        await converter.to_markdown(tmp_path / "never-downloaded.pdf")
