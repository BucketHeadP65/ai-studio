"""The errors the indexing slice raises to its callers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.indexing.domain.models import ValidationIssue


class CollectionIsForeignError(Exception):
    """An index aimed at a collection another knowledge base root indexed, without naming the collection.

    An unnamed collection must belong to this knowledge base's own root or to no root yet. Naming the
    collection (`collection_named=True`) takes it over.
    """

    def __init__(self, *, collection: str, stored_root: str, knowledge_base_root: str) -> None:
        """Name the collection and both roots.

        Args:
            collection: The collection the index aimed at.
            stored_root: The knowledge base root the collection was indexed from.
            knowledge_base_root: The root of the knowledge base being indexed.
        """
        self.collection = collection
        self.stored_root = stored_root
        self.knowledge_base_root = knowledge_base_root
        super().__init__(
            f"collection {collection} was indexed from {stored_root}, this knowledge base is at {knowledge_base_root}. "
            f"Name the collection explicitly to take it over, or index the knowledge base that owns it."
        )


class CollectionIsIndexedError(Exception):
    """Raised when `IngestPage` targets a collection that a knowledge base indexes.

    The next index of that collection deletes every page no indexed file produced, so the page would be
    lost. Ingest into a collection of its own that nothing indexes.
    """


class IngestValidationError(Exception):
    """Raised when `IngestPage` finds a vocabulary error in the page, before any write.

    `findings` carries every offending `ValidationIssue`, so a caller can name each unregistered value.
    """

    def __init__(self, findings: tuple[ValidationIssue, ...]) -> None:
        """Carry the findings that refused the write.

        Args:
            findings: Every vocabulary issue found in the page.
        """
        self.findings = findings
        super().__init__(f"{len(findings)} vocabulary finding(s) refused this page's write")


class ProposalUnknownError(Exception):
    """Raised by `approve`/`reject` when no pending row matches the given `(facet, value)`."""

    def __init__(self, facet: str, value: str) -> None:
        """Name the facet and value that matched no pending proposal.

        Args:
            facet: The facet of the proposal asked for.
            value: The proposed value asked for.
        """
        self.facet = facet
        self.value = value
        super().__init__(f"no pending vocab proposal for facet {facet!r} value {value!r}")
