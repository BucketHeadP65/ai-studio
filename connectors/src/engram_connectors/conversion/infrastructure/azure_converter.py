"""`DocumentConverter` adapter backed by Azure Document Intelligence (Managed Identity)."""

from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import DocumentAnalysisFeature, DocumentContentFormat, DocumentPage
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from engram.sources.contracts import ConversionError, ConversionResult
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path
    from typing import Protocol

    from core.settings import DocumentIntelligenceSettings

    # The part of the Azure DI client this adapter uses. `begin_analyze_document` mirrors the SDK signature.
    class _AnalyzeResult(Protocol):
        content: str | None
        pages: Sequence[DocumentPage] | None

    class _AnalyzePoller(Protocol):
        async def result(self) -> _AnalyzeResult: ...

    class _AnalyzeClient(Protocol):
        async def begin_analyze_document(
            self, model_id: str, *, body: object, output_content_format: object, features: object
        ) -> _AnalyzePoller: ...

    # A zero-arg factory yielding an open analyze client as an async context manager.
    ClientFactory = Callable[[], AbstractAsyncContextManager[_AnalyzeClient]]


class AzureAnalyzeResponse(BaseModel):
    """The fields of Azure DI's `AnalyzeResult` this adapter reads, validated from the SDK object's attributes.

    A result missing a field raises `ValidationError`.
    `content` is the markdown body and may be empty. `pages` is read only for its length.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", arbitrary_types_allowed=True)

    content: str | None
    pages: list[DocumentPage] | None = None


class AzureDocumentConverter:
    """`DocumentConverter` backed by Azure DI `prebuilt-layout`, authenticated with Managed Identity.

    It takes no API key. `DefaultAzureCredential` uses `az login` locally and the
    resource's managed identity in Azure. The identity needs the
    *Cognitive Services User* role on the Document Intelligence resource.
    """

    def __init__(self, settings: DocumentIntelligenceSettings, *, client_factory: ClientFactory | None = None) -> None:
        """Store the endpoint and the client factory.

        Args:
            settings: The Document Intelligence settings holding the endpoint.
            client_factory: Opens an analyze client. When `None`, a client authenticated with
                Managed Identity over `settings.endpoint` is used.
        """
        self._endpoint = settings.endpoint
        self._client_factory: ClientFactory = client_factory or self._managed_identity_client

    @asynccontextmanager
    async def _managed_identity_client(self) -> AsyncGenerator[_AnalyzeClient]:
        """Open a Document Intelligence client with `DefaultAzureCredential`, refusing an empty endpoint."""
        if not self._endpoint:
            msg = "Azure Document Intelligence endpoint is not configured (set infrastructure.document_intelligence.endpoint in config/application_settings.json)"
            raise ConversionError(msg)
        async with DefaultAzureCredential() as credential, DocumentIntelligenceClient(self._endpoint, credential) as client:
            yield cast("_AnalyzeClient", client)

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:  # noqa: ARG002 (part of the DocumentConverter port contract, DI converts the PDF and ignores the web hint)
        """Convert the PDF at `pdf_path` to markdown with Azure DI `prebuilt-layout`.

        Args:
            pdf_path: Absolute path to the PDF to analyze.
            source_url: Unused. Azure DI always converts the PDF.

        Returns:
            The Markdown content and its page count.

        Raises:
            ConversionError: The PDF is not on disk, Azure DI cannot be reached or refuses
                the request, or it returns no content for the PDF.
        """
        if not await asyncio.to_thread(pdf_path.is_file):
            msg = f"no PDF on disk at {pdf_path} for Azure DI to analyze"
            raise ConversionError(msg)
        try:
            async with self._client_factory() as client:
                with pdf_path.open("rb") as handle:
                    poller = await client.begin_analyze_document(
                        "prebuilt-layout",
                        body=handle,
                        output_content_format=DocumentContentFormat.MARKDOWN,
                        features=[DocumentAnalysisFeature.FORMULAS],
                    )
                result = await poller.result()
        except AzureError as error:
            msg = f"Azure DI could not analyze {pdf_path.name}: {error}"
            raise ConversionError(msg) from error
        response = AzureAnalyzeResponse.model_validate(result, from_attributes=True)
        if not response.content:
            msg = f"Azure DI returned empty content for {pdf_path.name}"
            raise ConversionError(msg)
        return ConversionResult(
            markdown=response.content, page_count=len(response.pages or []), converter="azure-di/prebuilt-layout+formulas"
        )
