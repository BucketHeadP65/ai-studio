"""The `engram.sources.converters` entry-point factories, one per connector name.

A host resolves each one through `engram.sources.registry`. Each takes only the Document Intelligence settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram_connectors.conversion.infrastructure.arxiv_html.converter import ArxivHtmlConverter
from engram_connectors.conversion.infrastructure.azure_converter import AzureDocumentConverter
from engram_connectors.conversion.infrastructure.hybrid import HybridConverter
from engram_connectors.conversion.infrastructure.passthrough import PassthroughConverter

if TYPE_CHECKING:
    from core.settings import DocumentIntelligenceSettings
    from engram.sources.contracts import DocumentConverter


def zotero_converter_factory(document_intelligence: DocumentIntelligenceSettings) -> DocumentConverter:
    """Build the `zotero` converter: arXiv HTML first, Azure Document Intelligence over the PDF as the fallback.

    Args:
        document_intelligence: The Document Intelligence settings the PDF fallback reads its endpoint from.

    Returns:
        The composed converter.
    """
    return HybridConverter(ArxivHtmlConverter(), AzureDocumentConverter(document_intelligence))


def youtube_converter_factory(document_intelligence: DocumentIntelligenceSettings) -> DocumentConverter:
    """Build the `youtube` converter, which reads back the transcript `YouTubeConnector` already rendered.

    Args:
        document_intelligence: Accepted for the entry-point shape and unused.

    Returns:
        A converter labelled `youtube-transcript`.
    """
    del document_intelligence
    return PassthroughConverter("youtube-transcript")


def source_queue_converter_factory(document_intelligence: DocumentIntelligenceSettings) -> DocumentConverter:
    """Build the `intake` converter, which reads back the markdown `SourceQueueConnector` already rendered.

    Args:
        document_intelligence: Accepted for the entry-point shape and unused.

    Returns:
        A converter labelled `intake`.
    """
    del document_intelligence
    return PassthroughConverter("intake")


def beir_converter_factory(document_intelligence: DocumentIntelligenceSettings) -> DocumentConverter:
    """Build the converter for BEIR mirrors, which reads each cached document file as its body.

    Args:
        document_intelligence: The document-conversion settings, which this converter does not need.

    Returns:
        A passthrough converter labelled `beir`.
    """
    del document_intelligence
    return PassthroughConverter("beir")


def multihop_rag_converter_factory(document_intelligence: DocumentIntelligenceSettings) -> DocumentConverter:
    """Build the converter for MultiHop-RAG mirrors, which reads each cached document file as its body.

    Args:
        document_intelligence: The document-conversion settings, which this converter does not need.

    Returns:
        A passthrough converter labelled `multihop_rag`.
    """
    del document_intelligence
    return PassthroughConverter("multihop_rag")
