"""The applied policy as a source of guidance, read by a host that is already serving people.

`policy_instructions` is one producer of the `guidance` voice a `SqlKnowledgeBase` speaks — the one engram
itself ships. `engram mcp` reads the active policy once and exits cleanly if it is malformed. A
running host cannot exit, so the rule here is different and worth pinning: a policy that appears
mid-session is picked up, and one that goes bad mid-session degrades to silence rather than taking
the session down.

The `k_source` pass-through is the second contract here: what a host hands the library build must
reach the reader it gets back, or the cap is a setting that silently does nothing. Composing a
reader is otherwise the integration tier's business — one step of it opens a connection (the
pgvector codec registration), so these stub that step and let the rest run for real; nothing here
queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import yaml

from sqlalchemy.exc import SQLAlchemyError

from engram.evals.tuning.policy import baseline_policy
from engram.indexing.contracts import IngestValidationError, RetrievalSwitches
from engram.kb.domain.schema import RetrievalSpec
from engram.retrieval.capability import SqlKnowledgeBase
from engram.retrieval.earned import Earned
from engram.retrieval.infrastructure import embedded
from engram.retrieval.infrastructure.embedded import (
    KnowledgeBaseConnection,
    embedded_authoring,
    embedded_knowledge_base,
    embedded_read_tools,
    embedded_retrieval,
    policy_instructions,
    switch_source,
)
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mcp.types import Tool
    from sqlalchemy.ext.asyncio import AsyncEngine

    from engram.kb.contracts import KnowledgeBaseSchema
    from engram.retrieval.domain import SchemaDescription, SqlResult

READER_DSN = "postgresql+asyncpg://reader:pw@localhost:5433/memory"
"""A well-formed reader DSN: the engine is built from it, never connected through it."""


def _apply(knowledge_base: Path, instructions: str) -> Path:
    """Write an active policy the way `evals apply-policy` does, and return its path."""
    policy = baseline_policy().model_copy(update={"instructions": instructions})
    path = knowledge_base / "_meta" / "active-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy.model_dump(mode="json")), encoding="utf-8")
    return path


def test_a_knowledge_base_with_no_policy_supplies_no_guidance(tmp_path: Path) -> None:
    assert policy_instructions(tmp_path)() is None


def test_the_applied_policys_instructions_become_the_guidance(tmp_path: Path) -> None:
    _apply(tmp_path, "Prefer api.search with k <= 8.")
    assert policy_instructions(tmp_path)() == "Prefer api.search with k <= 8."


def test_a_policy_applied_while_the_host_runs_takes_effect(tmp_path: Path) -> None:
    """The reason this is a callable and not a snapshot: `evals apply-policy` lands mid-session."""
    read = policy_instructions(tmp_path)
    assert read() is None
    _apply(tmp_path, "Ground facet values before filtering.")
    assert read() == "Ground facet values before filtering."


def test_a_policy_that_goes_bad_degrades_to_silence(tmp_path: Path) -> None:
    """A host mid-session logs it and speaks with the two voices that remain; it does not fall over."""
    path = _apply(tmp_path, "Prefer api.search with k <= 8.")
    read = policy_instructions(tmp_path)
    assert read() is not None
    path.write_text("strategy: a-strategy-the-serve-set-never-had\n", encoding="utf-8")
    assert read() is None


@pytest.fixture
def _unconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two steps of opening a reader that need a live database: the role check and the pgvector codec."""

    async def skip_registration(engine: AsyncEngine) -> None:
        del engine

    monkeypatch.setattr(embedded, "verify_reader_role", skip_registration)
    monkeypatch.setattr(embedded, "register_pgvector", skip_registration)


def _own_collection(index: int) -> KnowledgeBaseSchema:
    """The research spec under a collection of its own, since two doors may never claim one collection."""
    spec = research_spec()
    return spec.model_copy(
        update={"slug": f"research-{index}", "retrieval": spec.retrieval.model_copy(update={"collection": f"research-{index}"})}
    )


async def _library_search(door_count: int, k_source: Callable[[], int] | None) -> Tool:
    """Open a library of `door_count` knowledge bases under `k_source` and return its published `search`."""
    doors = tuple(KnowledgeBaseConnection(READER_DSN, spec=_own_collection(index)) for index in range(door_count))
    async with embedded_read_tools(*doors, k_source=k_source) as reader:
        [search] = [tool for tool in await reader.list_tools() if tool.name == "search"]
    return search


@pytest.mark.usefixtures("_unconnected")
class TestTheHostCapReachesTheReader:
    """A cap handed to the library build must land on the tools it yields, on every door count."""

    async def test_one_knowledge_base_under_a_cap_publishes_no_k(self):
        async with embedded_retrieval(READER_DSN, spec=research_spec(), k_source=lambda: 7) as reader:
            [search] = [tool for tool in await reader.list_tools() if tool.name == "search"]

        assert "query" in search.input_schema["properties"]
        assert "k" not in search.input_schema["properties"]

    async def test_one_knowledge_base_without_a_cap_keeps_the_agent_argued_k(self):
        async with embedded_retrieval(READER_DSN, spec=research_spec()) as reader:
            [search] = [tool for tool in await reader.list_tools() if tool.name == "search"]

        assert search.input_schema["properties"]["k"]["default"] == 12

    async def test_several_knowledge_bases_under_a_cap_publish_no_k(self):
        properties = (await _library_search(2, lambda: 7)).input_schema["properties"]

        assert "knowledge_base" in properties
        assert "k" not in properties

    async def test_several_knowledge_bases_without_a_cap_keep_the_agent_argued_k(self):
        properties = (await _library_search(2, None)).input_schema["properties"]

        assert properties["k"]["default"] == 12
        assert "knowledge_base" in properties

    async def test_the_cap_is_read_per_call_not_at_open(self):
        """The reason this is a callable: a knob a person turns mid-session lands on the next search."""
        cap = {"k": 5}
        async with embedded_retrieval(READER_DSN, spec=research_spec(), k_source=lambda: cap["k"]) as reader:
            assert reader.k_source is not None
            assert reader.k_source() == 5
            cap["k"] = 2
            assert reader.k_source() == 2


def _auto_spec() -> KnowledgeBaseSchema:
    return research_spec().model_copy(update={"retrieval": RetrievalSpec(mode="auto")})


@pytest.mark.usefixtures("_unconnected")
class TestSwitchSourceWiring:
    """Only an auto-mode door's surface gets a `switch_source` — the fixed-mode-invariance binding."""

    async def test_a_fixed_mode_spec_gets_no_switch_source(self):
        async with embedded_read_tools(KnowledgeBaseConnection(READER_DSN, spec=research_spec())) as reader:
            assert reader.surfaces[0].switch_source is None

    async def test_an_auto_mode_spec_gets_a_switch_source(self):
        async with embedded_read_tools(KnowledgeBaseConnection(READER_DSN, spec=_auto_spec())) as reader:
            assert reader.surfaces[0].switch_source is not None


@pytest.mark.usefixtures("_unconnected")
class TestEmbeddedKnowledgeBaseModeWiring:
    """`embedded_knowledge_base` wraps in `Earned` only for a full `KnowledgeBaseSchema` in auto mode.

    The auto-mode branch also seeds a read through the knowledge base's own gateway, so it needs a live
    database and is covered at the integration tier instead (`tests/integration/test_sql_door_earned_db.py`).
    """

    async def test_a_fixed_mode_spec_is_the_bare_sql_knowledge_base(self):
        async with embedded_knowledge_base(READER_DSN, domain="x", spec=research_spec()) as capability:
            assert isinstance(capability, SqlKnowledgeBase)

    async def test_no_spec_at_all_is_the_bare_sql_knowledge_base(self):
        async with embedded_knowledge_base(READER_DSN, domain="x") as capability:
            assert isinstance(capability, SqlKnowledgeBase)

    async def test_a_narrow_schema_spec_with_no_knowledge_base_schema_is_the_bare_sql_knowledge_base(self):
        """A duck-typed, `page_types`-only spec (a raw SQL knowledge base outside engram's own grammar).

        It has no `retrieval.mode` to consult at all, so it can never earn -- it stays fixed.
        """

        class _BareSchemaSpec:
            page_types: tuple[()] = ()
            slug: str = "bare"
            purpose: str = ""
            claims_declared: bool = False

        async with embedded_knowledge_base(READER_DSN, domain="x", spec=_BareSchemaSpec()) as capability:
            assert isinstance(capability, SqlKnowledgeBase)
            assert not isinstance(capability, Earned)


def _reads_as(capability: SqlKnowledgeBase[object] | Earned[object]) -> str | None:
    """The collection `embedded_knowledge_base`'s door announces itself as, read off the gateway it built."""
    assert isinstance(capability, SqlKnowledgeBase)
    assert isinstance(capability.gateway, ReadOnlySqlGateway)
    return capability.gateway.collection


@pytest.mark.usefixtures("_unconnected")
class TestEmbeddedKnowledgeBaseIdentity:
    """`spec` alone decides who the door is. `grounding` only decides what `describe_schema` names.

    The separation is what a door over several knowledge bases depends on: it must read
    anonymously and stay unearned no matter how many specs the render is grounded in, and no
    matter what retrieval mode those specs declare.
    """

    async def test_the_doors_own_spec_reads_as_that_knowledge_base(self):
        async with embedded_knowledge_base(READER_DSN, domain="x", spec=research_spec()) as capability:
            assert _reads_as(capability) == research_spec().retrieval.collection

    async def test_grounding_alone_reads_anonymously(self):
        """Grounding the render in several knowledge bases buys the door none of their identities."""
        grounding = (research_spec(), research_spec().model_copy(update={"slug": "other"}))

        async with embedded_knowledge_base(READER_DSN, domain="x", grounding=grounding) as capability:
            assert _reads_as(capability) is None

    async def test_grounding_in_one_auto_mode_spec_is_still_an_anonymous_bare_sql_knowledge_base(self):
        """The regression a count-based rule would reintroduce: one auto-mode base grounding a door it does not own.

        Counting the specs would hand this door that knowledge base's identity and wrap it in `Earned`,
        which a host relying on the bare `SqlKnowledgeBase` then trips over on every call. `spec=None`
        says the door owns no knowledge base, and nothing in `grounding` may overrule it.
        """
        auto = _auto_spec()

        async with embedded_knowledge_base(READER_DSN, domain="x", grounding=(auto,)) as capability:
            assert isinstance(capability, SqlKnowledgeBase)
            assert not isinstance(capability, Earned)
            assert _reads_as(capability) is None

    async def test_no_spec_at_all_reads_anonymously(self):
        async with embedded_knowledge_base(READER_DSN, domain="x") as capability:
            assert _reads_as(capability) is None


@dataclass
class _RaisingGateway:
    """A `SqlGateway` whose `rows` call always fails — the switch-read-fails-at-the-database path."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = 'relation "api.retrieval_switches" does not exist'
        raise SQLAlchemyError(msg)

    async def api_objects(self) -> SchemaDescription:
        raise NotImplementedError


class TestSwitchReadFailsClosed:
    """A DB-level failure reading the switch (missing function, missing grant) degrades to every switch off."""

    async def test_a_gateway_error_answers_every_switch_off_rather_than_raising(self):
        read_switches = switch_source(_RaisingGateway(), "research")

        assert await read_switches() == RetrievalSwitches()


WRITER_DSN = "postgresql+asyncpg://writer:pw@localhost:1/memory"
"""A writer DSN on a port nothing listens on: a write that got past the vocabulary gate would fail to connect."""


def _entity_page(entity_type: str) -> dict[str, object]:
    """An entity page payload under the research spec whose only vocabulary is its `entity_type`."""
    return {
        "frontmatter": {
            "id": "ent.evidence-audit",
            "type": "entity",
            "title": "Evidence audit",
            "summary": "A multi-pass audit over a contested claim.",
            "status": "draft",
            "entity_type": entity_type,
        },
        "sections": [
            {"heading": "Definition", "body": "Audit a contested claim across several retrieval strategies."},
            {"heading": "Mechanism", "body": "Search, fetch, weigh the contradictions, publish behind a gate."},
        ],
    }


@pytest.mark.usefixtures("_unconnected")
class TestEmbeddedAuthoringVocabularyGate:
    """`embedded_authoring` checks every page against the knowledge base's vocabulary before it opens a connection."""

    async def test_a_value_the_vocabulary_beside_the_spec_path_does_not_register_is_refused(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_vocabulary()
        spec_path = builder.add_knowledge_base_schema()

        async with embedded_authoring(WRITER_DSN, spec=spec_path, embedder=FakeEmbedding()) as ingest:
            with pytest.raises(IngestValidationError) as exc_info:
                await ingest(_entity_page("workflow"), collection="workflows")

        (finding,) = exc_info.value.findings
        assert finding.rule == "unknown-entity-type"
        assert "workflow" in finding.message

    async def test_a_value_the_vocabulary_beside_the_spec_path_registers_reaches_the_database(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_vocabulary()
        spec_path = builder.add_knowledge_base_schema()

        async with embedded_authoring(WRITER_DSN, spec=spec_path, embedder=FakeEmbedding()) as ingest:
            with pytest.raises(OSError):  # noqa: PT011 (the connection refusal on port 1 carries no stable message to match)
                await ingest(_entity_page("technique"), collection="workflows")

    async def test_a_spec_object_is_checked_against_an_empty_vocabulary(self):
        async with embedded_authoring(WRITER_DSN, spec=research_spec(), embedder=FakeEmbedding()) as ingest:
            with pytest.raises(IngestValidationError) as exc_info:
                await ingest(_entity_page("technique"), collection="workflows")

        (finding,) = exc_info.value.findings
        assert finding.rule == "unknown-entity-type"
