r"""Unit tests for the embedding adapters (`src/engram/indexing/infrastructure/embedder.py`).

Two surfaces:

* :class:`~engram.testing.fakes.embedding.FakeEmbedding` — the deterministic
  hash-seeded fake the rest of the suite arranges with; pinned here for its two
  load-bearing properties (same text → same 1024-vector; different text →
  different vector).
* :class:`~engram.indexing.infrastructure.embedder.PydanticAiEmbedder` — the real
  :class:`~core.ai.embedding.BatchEmbedder` adapter over a pydantic-ai
  `Embedder`. Driven here through a **stub Embedder injected at the constructor
  seam** (no network, no monkeypatching): the adapter must slice `texts` into
  `batch_size` chunks, call the Embedder once per chunk, and concatenate the
  per-chunk vectors back **in input order**. `model_id` / `dimensions` surface
  the settings the embed stage records in `kb.embed_meta`.

A single env-gated test exercises the real Azure path; it skips unless
`APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT` + `APP_INGESTION__EMBEDDING_DEPLOYMENT` are set
(so it skips in CI / tonight).
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.settings import AzureOpenAiSettings, DatabaseSettings, InfrastructureSettings

from engram.config import IngestionSettings
from engram.indexing.contracts import PydanticAiEmbedder
from engram.testing.fakes.embedding import FakeEmbedding

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.embeddings import EmbeddingResult, EmbeddingSettings


@dataclass
class _StubEmbedder:
    """Records each `embed_documents` call; returns index-derived 3-dim vectors.

    Stands in for `pydantic_ai.Embedder` at the adapter's constructor seam — its
    `embed_documents(documents, *, settings=...)` mirrors the real signature and
    returns the real :class:`EmbeddingResult` so the adapter's unwrapping is tested
    against the genuine result shape, not a hand-rolled stand-in.
    """

    dim: int = 3
    calls: list[list[str]] = field(default_factory=list)
    settings_seen: list[EmbeddingSettings | None] = field(default_factory=list)

    async def embed_documents(self, documents: Sequence[str], *, settings: EmbeddingSettings | None = None) -> EmbeddingResult:
        from pydantic_ai.embeddings import EmbeddingResult

        batch = list(documents)
        self.calls.append(batch)
        self.settings_seen.append(settings)
        # Encode each text's global position so order errors are detectable: the
        # first dim is a stable per-text fingerprint (its length here).
        embeddings = [[float(len(text))] * self.dim for text in batch]
        return EmbeddingResult(embeddings=embeddings, inputs=batch, input_type="document", model_name="stub", provider_name="stub")


def _embedder(
    stub: _StubEmbedder, *, batch_size: int = 64, batch_tokens: int | None = None, dimensions: int = 1024, deployment: str = "text-embedding-3-small"
) -> PydanticAiEmbedder:
    return PydanticAiEmbedder(stub, deployment=deployment, dimensions=dimensions, batch_size=batch_size, batch_tokens=batch_tokens)


class TestFakeEmbedding:
    """The shared deterministic fake — a real :class:`BatchEmbedder`."""

    def test_satisfies_embedding_port(self):
        assert isinstance(FakeEmbedding(), BatchEmbedder)

    def test_dimensions_and_model_id(self):
        fake = FakeEmbedding()

        assert fake.dimensions == 1024
        assert fake.model_id == "fake-embed"

    async def test_same_text_same_vector(self):
        fake = FakeEmbedding()

        first = (await fake.embed_batch(["graph rag"])).vectors
        second = (await fake.embed_batch(["graph rag"])).vectors

        assert first[0] == second[0]
        assert len(first[0]) == 1024

    async def test_different_text_different_vector(self):
        fake = FakeEmbedding()

        [vec_a, vec_b] = (await fake.embed_batch(["alpha", "beta"])).vectors

        assert vec_a != vec_b

    async def test_records_texts_and_batches(self):
        fake = FakeEmbedding()

        await fake.embed_batch(["a", "b"])
        await fake.embed_batch(["c"])

        assert fake.texts == ["a", "b", "c"]
        assert fake.batches == [["a", "b"], ["c"]]


class TestPydanticAiEmbedder:
    """The real adapter, driven through an injected stub Embedder (no network)."""

    def test_satisfies_embedding_port(self):
        embedder = _embedder(_StubEmbedder())

        assert isinstance(embedder, BatchEmbedder)

    def test_model_id_is_the_deployment(self):
        embedder = _embedder(_StubEmbedder(), deployment="text-embedding-3-large")

        assert embedder.model_id == "text-embedding-3-large"

    def test_dimensions_from_settings(self):
        embedder = _embedder(_StubEmbedder(), dimensions=1024)

        assert embedder.dimensions == 1024

    async def test_single_batch_preserves_order(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub, batch_size=64)

        vectors = (await embedder.embed_batch(["aa", "b", "ccc"])).vectors

        # Stub encodes len(text) into every component → order is verifiable.
        assert [vec[0] for vec in vectors] == [2.0, 1.0, 3.0]
        assert stub.calls == [["aa", "b", "ccc"]]

    async def test_slices_into_batch_size_chunks(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub, batch_size=2)

        await embedder.embed_batch(["a", "b", "c", "d", "e"])

        assert stub.calls == [["a", "b"], ["c", "d"], ["e"]]

    async def test_concatenates_chunks_in_input_order(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub, batch_size=2)

        vectors = (await embedder.embed_batch(["x", "yy", "zzz", "wwww"])).vectors

        assert [vec[0] for vec in vectors] == [1.0, 2.0, 3.0, 4.0]

    async def test_empty_input_makes_no_calls(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub)

        embeddings = await embedder.embed_batch([])

        assert embeddings.vectors == []
        assert embeddings.input_tokens == 0
        assert stub.calls == []

    async def test_passes_dimensions_to_the_embedder(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub, dimensions=1024)

        await embedder.embed_batch(["a"])

        assert stub.settings_seen[0] == {"dimensions": 1024}

    async def test_truncates_oversized_text_to_the_token_budget_before_reaching_the_inner_embedder(self):
        # Defensive belt: a builder that forgot to truncate must not crash the
        # index with HTTP 400 — the adapter truncates to the token budget before
        # the network call. The text reaching the inner embedder re-encodes to
        # exactly EMBED_TOKEN_BUDGET tokens.
        import tiktoken

        from engram.indexing.domain.card import EMBED_TOKEN_BUDGET

        encoding = tiktoken.get_encoding("cl100k_base")
        stub = _StubEmbedder()
        embedder = _embedder(stub)

        await embedder.embed_batch([" ".join(f"token{i}" for i in range(EMBED_TOKEN_BUDGET * 2))])

        [sent] = stub.calls
        assert len(encoding.encode(sent[0], disallowed_special=())) == EMBED_TOKEN_BUDGET

    async def test_groups_requests_by_engram_token_count(self):
        # "graph rag" is two cl100k tokens, so a three-token cap closes each request after one text.
        stub = _StubEmbedder()
        embedder = _embedder(stub, batch_tokens=3)

        await embedder.embed_batch(["graph rag", "graph rag", "graph rag"])

        assert stub.calls == [["graph rag"], ["graph rag"], ["graph rag"]]

    async def test_undersized_text_passes_through_byte_identical(self):
        stub = _StubEmbedder()
        embedder = _embedder(stub)

        await embedder.embed_batch(["graph rag"])

        assert stub.calls == [["graph rag"]]


@pytest.mark.skipif(
    not (os.getenv("APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT") and os.getenv("APP_INGESTION__EMBEDDING_DEPLOYMENT")),
    reason="APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT + APP_INGESTION__EMBEDDING_DEPLOYMENT required for the real Azure embedding call",
)
async def test_real_azure_embedding_roundtrip():
    infrastructure = InfrastructureSettings(
        database=DatabaseSettings(url="postgresql+asyncpg://o:p@localhost/kb", reader_url="postgresql+asyncpg://r:p@localhost/kb"),
        azure_openai=AzureOpenAiSettings(endpoint=os.environ["APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT"]),
    )
    ingestion = IngestionSettings(embedding_deployment=os.environ["APP_INGESTION__EMBEDDING_DEPLOYMENT"])
    embedder = PydanticAiEmbedder.from_settings(infrastructure, ingestion)

    vectors = (await embedder.embed_batch(["the quick brown fox", "graph-based retrieval"])).vectors

    assert len(vectors) == 2
    assert all(len(vec) == ingestion.embedding_dimensions for vec in vectors)
    assert vectors[0] != vectors[1]
