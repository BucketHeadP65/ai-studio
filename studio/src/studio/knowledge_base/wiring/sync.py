"""What the sync slice registers: the sync handler, one mirror sync per connector the knowledge base declares.

The composition root routes the sync command to it on the shared message bus.
"""

from __future__ import annotations

from engram.kb.contracts import KnowledgeBaseSchema, read_policy
from engram.sources.contracts import MirrorStore, MultiConnectorSync, SyncMirrorsHandler
from wireup import injectable

from studio.knowledge_base.settings import KnowledgeBaseSettings
from studio.knowledge_base.wiring.conversion import DocumentConverterMap
from studio.knowledge_base.wiring.sources import ConnectorMap


@injectable
def sync_handler_provider(
    connectors: ConnectorMap, converters: DocumentConverterMap, store: MirrorStore, schema: KnowledgeBaseSchema, knowledge_base: KnowledgeBaseSettings
) -> MultiConnectorSync:
    """Provide the sync handler: one mirror sync per declared connector, over the shared mirror store.

    Every connector's sync carries the `max_age_years` rule of the knowledge base's ingestion policy, read from its policy page
    over the schema's defaults, so an item older than the rule is refused at admission the way a discovered reference is.

    Args:
        connectors: The declared connectors, keyed by name.
        converters: The declared document converters, keyed by name.
        store: The mirror store every sync writes to.
        schema: The knowledge base's schema, whose defaults the policy page overrides.
        knowledge_base: The knowledge base whose policy page is read.

    Returns:
        The sync handler over every declared connector.
    """
    max_age_years = read_policy(knowledge_base.path, schema).rules.max_age_years
    return MultiConnectorSync(
        {
            name: SyncMirrorsHandler(connectors.by_key[name], converters.by_key[name], store, max_age_years=max_age_years)
            for name in connectors.by_key
        }
    )


REGISTRATIONS: list[object] = [sync_handler_provider]
"""Everything this slice puts in the container, each under the type its consumers declare."""
