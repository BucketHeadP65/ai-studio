"""What the conversion slice registers: every document converter the knowledge base's schema declares.

Each name resolves through the entry points of the installed connectors, so adding a converter never needs an edit here.
"""

from __future__ import annotations

from core.ai.registry import Registry
from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import ConnectorNotInstalledError, DocumentConverter, available_converters, converter_factory
from wireup import injectable

from studio.knowledge_base.settings import DocumentIntelligenceSettings


class DocumentConverterMap(Registry[str, DocumentConverter]):
    """Every document converter a knowledge base declares in `sources.connectors`, keyed by name."""


@injectable
def document_converters_provider(schema: KnowledgeBaseSchema, document_intelligence: DocumentIntelligenceSettings) -> DocumentConverterMap:
    """Provide every document converter the knowledge base's schema declares, by name.

    Args:
        schema: The knowledge base's schema, whose `sources.connectors` names the converters.
        document_intelligence: The Document Intelligence resource a converter may build on.

    Returns:
        The declared converters, keyed by name.

    Raises:
        ValueError: When a declared name has no installed converter. The message names the knowledge base and every installed converter.
    """
    built: dict[str, DocumentConverter] = {}
    for name in schema.sources.connectors:
        try:
            built[name] = converter_factory(name)(document_intelligence)
        except ConnectorNotInstalledError as error:
            installed = ", ".join(available_converters()) or "none"
            msg = f"knowledge base {schema.slug!r} declares sources.connectors {name!r}, but {error} (available converters are {installed})"
            raise ValueError(msg) from error
    return DocumentConverterMap(built)


REGISTRATIONS: list[object] = [document_converters_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
