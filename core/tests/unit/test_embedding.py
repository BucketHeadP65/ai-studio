"""The grouped, concurrent embedding adapter: how texts become requests, how many run at once, and what comes back."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from pydantic_ai.embeddings import EmbeddingResult
from pydantic_ai.usage import RequestUsage

from core.ai.embedding import BatchEmbedder, BatchQueryEmbedder, Embeddings, PydanticAiEmbedder, query_model_of, split_into_requests

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.embeddings import EmbeddingSettings


def _vector_of(text: str) -> list[float]:
    return [float(len(text)), float(ord(text[0])) if text else 0.0]


class RecordingEmbedder:
    """Answers each request with one vector per text, recording requests and the peak number in flight."""

    def __init__(self, *, delays: dict[str, float] | None = None, fail_on: str | None = None, tokens_per_text: int = 3) -> None:
        self.requests: list[list[str]] = []
        self.in_flight = 0
        self.peak = 0
        self.delays = delays or {}
        self.fail_on = fail_on
        self.tokens_per_text = tokens_per_text

    async def embed_documents(self, documents: Sequence[str], *, settings: EmbeddingSettings | None = None) -> EmbeddingResult:
        del settings
        self.requests.append(list(documents))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(max((self.delays.get(text, 0.0) for text in documents), default=0.0))
            if self.fail_on in documents:
                msg = f"refused {self.fail_on}"
                raise RuntimeError(msg)
            return EmbeddingResult(
                [_vector_of(text) for text in documents],
                inputs=list(documents),
                input_type="document",
                model_name="test",
                provider_name="test",
                usage=RequestUsage(input_tokens=self.tokens_per_text * len(documents)),
            )
        finally:
            self.in_flight -= 1


def _adapter(embedder: RecordingEmbedder, *, batch_size: int, batch_tokens: int | None = None, concurrency: int = 1) -> PydanticAiEmbedder:
    return PydanticAiEmbedder(
        embedder,
        deployment="embed",
        dimensions=2,
        batch_size=batch_size,
        batch_tokens=batch_tokens,
        concurrency=concurrency,
        count_tokens=len,
    )


class TestSplitting:
    def test_a_request_closes_at_the_text_cap(self) -> None:
        assert split_into_requests([1] * 5, batch_size=2, batch_tokens=None) == [range(2), range(2, 4), range(4, 5)]

    def test_a_request_closes_before_the_token_cap(self) -> None:
        assert split_into_requests([4, 4, 4, 1], batch_size=10, batch_tokens=9) == [range(2), range(2, 4)]

    def test_a_text_over_the_token_cap_goes_alone(self) -> None:
        assert split_into_requests([2, 20, 2], batch_size=10, batch_tokens=5) == [range(1), range(1, 2), range(2, 3)]

    def test_no_texts_make_no_requests(self) -> None:
        assert split_into_requests([], batch_size=3, batch_tokens=5) == []


class TestEmbedBatch:
    async def test_vectors_come_back_in_input_order_when_requests_finish_out_of_order(self) -> None:
        embedder = RecordingEmbedder(delays={"slow": 0.05})
        texts = ["slow", "b", "cc", "ddd"]

        result = await _adapter(embedder, batch_size=1, concurrency=4).embed_batch(texts)

        assert result.vectors == [_vector_of(text) for text in texts]

    async def test_no_more_than_the_concurrency_limit_are_in_flight(self) -> None:
        embedder = RecordingEmbedder(delays={str(index): 0.01 for index in range(12)})

        await _adapter(embedder, batch_size=1, concurrency=3).embed_batch([str(index) for index in range(12)])

        assert embedder.peak == 3
        assert len(embedder.requests) == 12

    async def test_requests_are_grouped_by_the_token_cap(self) -> None:
        embedder = RecordingEmbedder()

        await _adapter(embedder, batch_size=10, batch_tokens=5).embed_batch(["aaa", "bb", "cccc", "d"])

        assert embedder.requests == [["aaa", "bb"], ["cccc", "d"]]

    async def test_the_token_cap_needs_a_counter(self) -> None:
        embedder = RecordingEmbedder()
        adapter = PydanticAiEmbedder(embedder, deployment="embed", dimensions=2, batch_size=10, batch_tokens=1)

        await adapter.embed_batch(["aaa", "bbb", "ccc"])

        assert embedder.requests == [["aaa", "bbb", "ccc"]]

    async def test_input_tokens_are_summed_over_requests(self) -> None:
        embedder = RecordingEmbedder(tokens_per_text=7)

        result = await _adapter(embedder, batch_size=2, concurrency=2).embed_batch(["a", "b", "c"])

        assert result.input_tokens == 21

    async def test_no_texts_send_nothing(self) -> None:
        embedder = RecordingEmbedder()

        assert await _adapter(embedder, batch_size=2).embed_batch([]) == Embeddings(vectors=[], input_tokens=0)
        assert embedder.requests == []

    async def test_a_failed_request_raises_its_own_error(self) -> None:
        embedder = RecordingEmbedder(fail_on="bad", delays={"slow": 0.2})

        with pytest.raises(RuntimeError, match="refused bad"):
            await _adapter(embedder, batch_size=1, concurrency=2).embed_batch(["slow", "bad"])

    async def test_truncation_runs_before_counting_and_sending(self) -> None:
        embedder = RecordingEmbedder()
        adapter = PydanticAiEmbedder(
            embedder, deployment="embed", dimensions=2, batch_size=10, batch_tokens=4, count_tokens=len, truncate=lambda text: text[:2]
        )

        await adapter.embed_batch(["aaaa", "bbbb", "cccc"])

        assert embedder.requests == [["aa", "bb"], ["cc"]]

    def test_it_is_an_embedding_port(self) -> None:
        assert isinstance(_adapter(RecordingEmbedder(), batch_size=1), BatchEmbedder)

    @pytest.mark.parametrize(
        ("name", "batch_size", "batch_tokens", "concurrency"),
        [("batch_size", 0, None, 1), ("batch_tokens", 1, 0, 1), ("concurrency", 1, None, 0)],
    )
    def test_limits_below_one_are_refused(self, name: str, batch_size: int, batch_tokens: int | None, concurrency: int) -> None:
        with pytest.raises(ValueError, match=name):
            _adapter(RecordingEmbedder(), batch_size=batch_size, batch_tokens=batch_tokens, concurrency=concurrency)


class TestQueryModel:
    async def test_a_port_query_embedder_names_the_ports_model_and_embeds_one_text(self):
        recording = RecordingEmbedder()
        embed = BatchQueryEmbedder(_adapter(recording, batch_size=8))

        vector = await embed("graph rag")

        assert query_model_of(embed) == "embed"
        assert recording.requests == [["graph rag"]]
        assert list(vector) == _vector_of("graph rag")

    async def test_a_plain_function_names_no_model(self):
        async def embed(text: str, /) -> list[float]:
            del text
            return [0.5]

        assert query_model_of(embed) is None
        assert query_model_of(None) is None
