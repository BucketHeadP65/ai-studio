"""The ports evaluation dataset generation depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.embedding import Embeddings

    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.coverage import StratumInventory
    from engram.evals.generation.domain.datasets import AuthoredCase, EvaluationDataset
    from engram.evals.generation.domain.drafting import SeedCaseDraft
    from engram.evals.shared.contracts import QaDifficulty, Seed


class SupportsSeeding(Protocol):
    """The structural seed draws an archetype seeder calls, one per archetype.

    Every draw orders its candidates by salted hash, so the same salt over an unchanged knowledge base
    gives the same seeds. A seed's gold ids come from the graph's structure.
    """

    async def pinpoint_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw one-source `pinpoint` seeds, each gold on the one page that owns a claim.

        Every pinpoint seed has one fixed difficulty, so a `band` asking for another returns nothing.

        Args:
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only seeds anchored on a page of this page type, or `None` for any.

        Returns:
            Up to `limit` pinpoint seeds, in draw order.
        """
        ...

    async def survey_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw `survey` seeds, each gold on a hub page and the pages whose edges of one relation point into it.

        Args:
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only seeds anchored on a page of this page type, or `None` for any.

        Returns:
            Up to `limit` survey seeds, in draw order.
        """
        ...

    async def trace_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw multi-hop `trace` seeds, each gold on the nodes of one `src -> mid -> dst` path.

        Every trace seed has one fixed difficulty, so a `band` asking for another returns nothing.

        Args:
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only seeds anchored on a page of this page type, or `None` for any.

        Returns:
            Up to `limit` trace seeds, in draw order, each over an acyclic path of three distinct pages.
        """
        ...

    async def synthesis_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw `synthesis` seeds, each gold on the distinct pages one page reaches over one relation.

        Args:
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only seeds anchored on a page of this page type, or `None` for any.

        Returns:
            Up to `limit` synthesis seeds, in draw order. Each seed's `n_sections` is the source page's
            section count, at least 1.
        """
        ...

    async def passage_seeds(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw one-section `passage` seeds, each gold on the page owning a section of at least `MIN_PASSAGE_CHARS` characters.

        A section of a meta page is never drawn. A seed's source text is the section's body alone. Every
        passage seed has one fixed difficulty, so a `band` asking for another returns nothing.

        Args:
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to sections of these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only sections of a page of this page type, or `None` for any.

        Returns:
            Up to `limit` passage seeds, in draw order. Empty when no section qualifies.
        """
        ...


class SeedFn(Protocol):
    """The seed draw an archetype resolves to, called with the seed reader to draw from."""

    async def __call__(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
        self,
        repo: SupportsSeeding,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        """Draw up to `limit` seeds of one archetype from `repo`.

        Args:
            repo: The seed reader to draw from.
            collection: The knowledge base to draw from.
            page_ids: Restrict the draw to these pages, or `()` for the whole knowledge base.
            limit: The most seeds to return.
            salt: The salt mixed into the hash order.
            band: Draw only seeds of this difficulty, or `None` for every difficulty.
            anchor_page_type: Draw only seeds anchored on a page of this page type, or `None` for any.

        Returns:
            Up to `limit` seeds, in draw order.
        """
        ...


class CaseDrafter(Protocol):
    """The drafter that writes a seed's cases: each one's question, answer and supporting quotes."""

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        """Draft up to `target` cases from `seed`.

        The drafts come back without a quote check.

        Args:
            seed: The seed whose source bodies ground the drafts.
            instructions: The drafting system prompt for the seed's archetype.
            target: How many cases to aim for. Fewer may come back.

        Returns:
            The drafts, in the order they were written.
        """
        ...


class SupportsEmbedBatch(Protocol):
    """Embeds a batch of texts, which the curator uses to find near-duplicate questions."""

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        """Embed a batch of texts.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per text, in input order, and the input tokens they cost.
        """
        ...


class SupportsInventory(Protocol):
    """Counts a knowledge base's seed pool per stratum."""

    async def inventory(self, *, collection: str) -> StratumInventory:
        """Count the seed pool of a knowledge base per stratum.

        Args:
            collection: The knowledge base to count.

        Returns:
            The count of available seeds for each stratum.
        """
        ...


class SupportsCuration(Protocol):
    """Curates drafted cases: drops near-duplicates, then samples them to a quota allocation."""

    async def dedup(self, cases: list[AuthoredCase]) -> list[AuthoredCase]:
        """Drop near-duplicate cases.

        Args:
            cases: The drafted cases.

        Returns:
            The cases that survive, in input order.
        """
        ...

    def stratify_cells(self, cases: list[AuthoredCase], allocation: QuotaAllocation) -> list[AuthoredCase]:
        """Sample the deduplicated cases to `allocation`, capping each stratum at its quota.

        Args:
            cases: The deduplicated cases.
            allocation: The quota of each stratum.

        Returns:
            The sampled cases.
        """
        ...


class QaGenerationQueue(Protocol):
    """The durable queue of page ids waiting for `studio evals gen-dataset` to generate their cases."""

    async def enqueue(self, page_ids: Sequence[str]) -> None:
        """Add page ids to the queue. An id already queued stays queued once.

        Args:
            page_ids: The page ids to add.
        """
        ...


class LogfireDatasetClient(Protocol):
    """The part of logfire's `LogfireAPIClient` that publishes a dataset."""

    def push_dataset(self, dataset: EvaluationDataset, *, name: str | None = None) -> object:
        """Publish `dataset` to the hosted Logfire datasets API, updating its cases by name.

        Args:
            dataset: The dataset to publish.
            name: The hosted dataset's name, or `None` for `dataset.name`.

        Returns:
            The hosted dataset's metadata.
        """
        ...
