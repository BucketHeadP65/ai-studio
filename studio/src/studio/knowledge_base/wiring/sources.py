"""What the sources slice registers: the resources every connector and scanner builds from, and every declared connector.

Each connector name resolves through the entry points of the installed connectors, so adding a connector never needs an edit here.
"""

from __future__ import annotations

from core.ai.registry import Registry
from engram.config import SourceResources
from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import Connector, ConnectorNotInstalledError, available_connectors, connector_class
from wireup import injectable

from studio.knowledge_base.settings import DocumentIntelligenceSettings, KnowledgeBaseSettings, SourceSettings


class ConnectorMap(Registry[str, Connector]):
    """Every connector a knowledge base declares in `sources.connectors`, keyed by name.

    It holds exactly the declared connectors, in declared order, so a caller that asks each connector in turn reads them from here.
    """


@injectable
def source_resources_provider(
    schema: KnowledgeBaseSchema, knowledge_base: KnowledgeBaseSettings, document_intelligence: DocumentIntelligenceSettings, sources: SourceSettings
) -> SourceResources:
    """Provide the one set of resources every connector's and every scanner's `build` takes.

    The Document Intelligence resource crosses as `None` when its endpoint is empty, so a converter that needs it refuses by name
    instead of building against an endpoint nobody set up. The source settings cross whole and unread, so each connector decides
    at build time whether its own settings count as configured. The resources carry the knowledge base's declared connector names,
    so whatever classifies a URL the way this knowledge base does never reads the schema from disk.

    Args:
        schema: The knowledge base's schema, whose `sources.connectors` names the connectors.
        knowledge_base: The knowledge base the resources belong to.
        document_intelligence: The Document Intelligence resource.
        sources: The per-connector settings blocks.

    Returns:
        The resources every connector and scanner builds from.
    """
    return SourceResources(
        knowledge_base=knowledge_base,
        document_intelligence=document_intelligence if document_intelligence.endpoint else None,
        connectors=tuple(schema.sources.connectors),
        settings=sources.root,
    )


@injectable
def connectors_provider(schema: KnowledgeBaseSchema, resources: SourceResources) -> ConnectorMap:
    """Provide every connector the knowledge base's schema declares, by name.

    The map holds exactly the declared connectors, in the order the schema declares them.

    Args:
        schema: The knowledge base's schema, whose `sources.connectors` names the connectors.
        resources: The resources every connector builds from.

    Returns:
        The declared connectors in declared order, keyed by name.

    Raises:
        ValueError: When a declared name has no installed connector. The message names the knowledge base and every installed connector.
    """
    built: dict[str, Connector] = {}
    for name in schema.sources.connectors:
        try:
            built[name] = connector_class(name).build(resources)
        except ConnectorNotInstalledError as error:
            installed = ", ".join(available_connectors()) or "none"
            msg = f"knowledge base {schema.slug!r} declares sources.connectors {name!r}, but {error} (available connectors are {installed})"
            raise ValueError(msg) from error
    return ConnectorMap(built)


REGISTRATIONS: list[object] = [source_resources_provider, connectors_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
