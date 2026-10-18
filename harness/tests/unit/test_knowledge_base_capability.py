"""The knowledge base capability: the tools and the words that make them usable travel together.

The claim under test is the contract port's ruling applied to the knowledge bases -- the agent is
an agnostic orchestrator, and what a knowledge base IS is the knowledge base machinery's to say. The negative half
matters as much: the agent's own instructions carry no knowledge base lore.

Most of that machinery is engram's now (`RetrievalKnowledgeBase` ships the read tools with each knowledge base's
domain and guidance). What is asserted here is what this app is still responsible for: that
engram's voices reach the agent intact, that the one thing engram cannot know -- where the
knowledge bases sit among this studio's other surfaces -- is stated, and that none of it has crept
back into the agent's own text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from engram import RetrievalKnowledgeBase, SqlKnowledgeBase
from engram.authoring.contracts import AuthoringKnowledgeBase
from engram.kb.domain.schema import RetrievalSpec
from engram.retrieval.capability import ReadSurface
from engram.retrieval.envelope import BadRefError
from engram.retrieval.infrastructure import embedded
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.tools import TOOL_NAMES, RetrievalHandlers
from engram.testing.specs import research_spec
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

import harness.adapters.knowledge_base

from harness.adapters.knowledge_base import KnowledgeBaseReading, _knowledge_base_domain, knowledge_base_capability
from harness.features.authoring.instructions import INSTRUCTIONS
from harness.knowledge_base import KnowledgeBase
from harness.settings import KNOWLEDGE_BASE_GUIDANCE, knowledge_base_guidance
from tests.support.fakes.about import FakeDomainReaderGateway
from tests.support.fakes.studio_registry import FakeStudioApi

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from core.ai.disclosure.contracts import DisclosureMode
    from engram.kb.contracts import KnowledgeBaseSchema

    from harness.adapters.studio_http import StudioApi

_DOMAIN = "A fixture knowledge base's domain text, arbitrary for these tests."


class _NeverAnswers:
    """One object satisfying every handler slice; these tests are about the words, never a call."""

    async def __call__(self, query: object, *, tools: frozenset[str] = TOOL_NAMES) -> Any:  # noqa: ANN401 (one fake satisfies four differently-returning slices)
        del query, tools
        msg = "these tests never call a handler"
        raise AssertionError(msg)


class _RootOnly:
    """An envelope reader that serves exactly its collection map."""

    mode: DisclosureMode = "strict"

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        if ref == "collection:research":
            return DisclosureEnvelope(ref="collection:research", kind="collection", title="research", summary="the map", mode="strict")
        msg = f"ref {ref!r} is outside this surface"
        raise BadRefError(msg)


def _capability(guidance: Callable[[], str | None] | None = None) -> KnowledgeBase[None]:
    """The studio's knowledge base capability over engram's, wired the way `harness_session` wires it."""
    silent = _NeverAnswers()
    return KnowledgeBase[None](
        knowledge_base=RetrievalKnowledgeBase[None](
            surfaces=(
                ReadSurface(
                    name="research",
                    collection="research",
                    handlers=RetrievalHandlers(search=silent, fetch=silent, graph_expand=silent, find_paths=silent),
                    envelope=_RootOnly(),
                    domain=_DOMAIN,
                    guidance=guidance,
                ),
            )
        )
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def test_the_knowledge_base_speaks_for_itself_and_the_agent_text_does_not() -> None:
    """The lore travels with the tools, and the agent text carries none of it.

    A field test showed the cost of the alternative: knowledge base lore in the agent text drifted with
    the agent, and the plugin that drifted answered a knowledge base question from parametric memory.
    """
    text = _capability().get_instructions()(_ctx())
    assert _DOMAIN in text, "what the knowledge base holds is the capability's to say"
    assert _capability().get_toolset() is not None

    assert "run_sql" not in INSTRUCTIONS, "the agent text carries work discipline, not capability lore"
    assert _DOMAIN not in INSTRUCTIONS


def test_the_studio_states_only_what_engram_cannot_know() -> None:
    """Engram has never heard of the store, of steps or of artifacts, so those boundaries are ours."""
    text = _capability().get_instructions()(_ctx())
    assert "The library is not one of them" in text
    assert "fetch_workflow" in text
    assert text.index(_DOMAIN) < text.index("The library is not one of them"), (
        "the knowledge base explains itself first; where it sits among our tools comes after"
    )


def test_the_deployments_domain_sentence_leads() -> None:
    """What this knowledge base HOLDS is a deployment fact, and the agent is told it before anything else."""
    assert _capability().get_instructions()(_ctx()).startswith(_DOMAIN)


def test_the_deployments_guidance_rides_with_the_domain() -> None:
    """How to retrieve well here travels beside what the knowledge base holds, and is read per request.

    Whoever authored it: this deployment's source is the checkout's applied `RetrievalPolicy`,
    read off the applied-policy file lazily, so guidance that changes mid-process takes effect
    on the next request.
    """
    spoken: list[str | None] = [None]
    capability = _capability(guidance=lambda: spoken[0])
    silent = capability.get_instructions()(_ctx())
    assert "Prefer search with k <= 8" not in silent

    spoken[0] = "Prefer search with k <= 8; open one unit at a time."
    guided = capability.get_instructions()(_ctx())
    assert guided.index(_DOMAIN) < guided.index("Prefer search with k <= 8") < guided.index("The library")


class _CompositionStoppedError(Exception):
    """Raised by the `embedded_knowledge_base` probe once it has recorded how the composition called it."""


@dataclass
class _EmbeddedKnowledgeBaseProbe:
    """Stands in for `embedded_knowledge_base`, recording what the composition asked for before stopping.

    A callable that captures the composition's arguments and returns itself as the async context
    manager, which then refuses to open (no live database in this tier) rather than actually
    connecting.
    """

    spec: KnowledgeBaseSchema | None = None
    grounding: tuple[KnowledgeBaseSchema, ...] = ()
    domain: str | None = None
    guidance: Callable[[], str | None] | None = None
    captured_guidance: bool = False

    def __call__(
        self,
        reader_url: str,
        *,
        domain: str,
        embed_query: Callable[[str], Any] | None = None,
        guidance: Callable[[], str | None] | None = None,
        tool_names: tuple[str, str] = ("run_sql", "describe_schema"),
        spec: KnowledgeBaseSchema | None = None,
        grounding: Sequence[KnowledgeBaseSchema] = (),
    ) -> _EmbeddedKnowledgeBaseProbe:
        del reader_url, embed_query, tool_names
        self.domain = domain
        self.spec = spec
        self.grounding = tuple(grounding)
        self.guidance = guidance
        self.captured_guidance = True
        return self

    async def __aenter__(self) -> NoReturn:
        raise _CompositionStoppedError

    async def __aexit__(self, *details: object) -> None:
        del details


def _knowledge_base(name: str, spec: KnowledgeBaseSchema) -> AuthoringKnowledgeBase:
    """One registry row as `knowledge_base_capability` reads it: named `name`, collection `name`, spec `spec`."""
    return AuthoringKnowledgeBase(name=name, collection=name, path=Path("/nowhere") / name, schema=spec)


def _two_knowledge_bases() -> tuple[AuthoringKnowledgeBase, ...]:
    """Two knowledge bases with distinguishable slugs, so a render can be traced back to each one."""
    research = research_spec()
    return (_knowledge_base("research", research), _knowledge_base("repo-docs", research.model_copy(update={"slug": "repo-docs"})))


@dataclass
class _StoppedProbe:
    """A probe already wired into `harness.adapters.knowledge_base`, plus the gateway the composition read through."""

    probe: _EmbeddedKnowledgeBaseProbe = field(default_factory=_EmbeddedKnowledgeBaseProbe)
    gateway: FakeDomainReaderGateway = field(default_factory=FakeDomainReaderGateway)


def _reads(knowledge_bases: Sequence[AuthoringKnowledgeBase]) -> Callable[[StudioApi], Awaitable[Sequence[AuthoringKnowledgeBase]]]:
    """Stands in for the registry read, so these tests never reach a studio."""

    async def read(client: StudioApi) -> Sequence[AuthoringKnowledgeBase]:
        del client
        return tuple(knowledge_bases)

    return read


_READER_DSN = "postgresql+asyncpg://reader:pw@localhost:5433/knowledge_bases"
"""A well-formed reader DSN: the engine is built from it, never connected through it."""


async def _open_stopped(
    monkeypatch: pytest.MonkeyPatch, *, knowledge_bases: Sequence[AuthoringKnowledgeBase], bodies: dict[str, str] | None = None
) -> _StoppedProbe:
    """Run `knowledge_base_capability` up to the moment it would open a real knowledge base, and report what it asked for."""
    stopped = _StoppedProbe(gateway=FakeDomainReaderGateway(bodies=bodies or {}))
    monkeypatch.setattr(harness.adapters.knowledge_base, "embedded_knowledge_base", stopped.probe)

    with pytest.raises(_CompositionStoppedError):
        async with knowledge_base_capability(
            FakeStudioApi(),
            gateway=lambda: stopped.gateway,
            knowledge_bases=_reads(knowledge_bases),
            reader_dsn=lambda: _READER_DSN,
            reading=KnowledgeBaseReading(guidance=KNOWLEDGE_BASE_GUIDANCE),
        ):
            pytest.fail("the probe stops the composition before the capability is usable")

    return stopped


async def test_the_domain_is_one_paragraph_per_knowledge_base_in_registry_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each knowledge base speaks its own indexed `api.about` body, named by the collection a `run_sql` filter takes."""
    knowledge_bases = _two_knowledge_bases()
    bodies = {"research": "A knowledge base about retrieval.", "repo-docs": "This repo's own working docs."}

    stopped = await _open_stopped(monkeypatch, knowledge_bases=knowledge_bases, bodies=bodies)

    assert stopped.probe.domain == "research: A knowledge base about retrieval.\n\n---\n\nrepo-docs: This repo's own working docs."


async def test_the_blocks_stay_separable_when_a_body_is_itself_several_paragraphs(monkeypatch: pytest.MonkeyPatch) -> None:
    """An indexed `api.about` body is markdown prose, so a blank line alone cannot mark where one base ends."""
    bodies = {"research": "First paragraph.\n\nSecond paragraph.", "repo-docs": "This repo's own working docs."}

    stopped = await _open_stopped(monkeypatch, knowledge_bases=_two_knowledge_bases(), bodies=bodies)

    assert stopped.probe.domain is not None
    assert stopped.probe.domain.split("\n\n---\n\n") == [
        "research: First paragraph.\n\nSecond paragraph.",
        "repo-docs: This repo's own working docs.",
    ]


async def test_a_knowledge_base_with_nothing_indexed_says_so_rather_than_borrowing_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge_bases = _two_knowledge_bases()

    stopped = await _open_stopped(monkeypatch, knowledge_bases=knowledge_bases, bodies={"research": "A knowledge base about retrieval."})

    assert stopped.probe.domain is not None
    assert "repo-docs: 'repo-docs' has not indexed an orientation page yet." in stopped.probe.domain


async def test_describe_schema_is_grounded_in_every_targets_own_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """One spec per knowledge base, in the same order, so `describe_schema` can name each one's page kinds."""
    knowledge_bases = _two_knowledge_bases()

    stopped = await _open_stopped(monkeypatch, knowledge_bases=knowledge_bases)

    assert stopped.probe.spec is None, "the plugin owns none of them, so none of them may lend it an identity"
    assert [spec.slug for spec in stopped.probe.grounding] == ["research", "repo-docs"]
    assert all(spec.page_types for spec in stopped.probe.grounding), "the threaded specs carry real page types, never stand-ins"


async def test_the_door_reads_every_targets_about_row(monkeypatch: pytest.MonkeyPatch) -> None:
    knowledge_bases = _two_knowledge_bases()

    stopped = await _open_stopped(monkeypatch, knowledge_bases=knowledge_bases)

    assert [params for _sql, params in stopped.gateway.calls] == [{"collection": "research"}, {"collection": "repo-docs"}]


async def test_the_guidance_is_the_deployments_authored_floor_with_no_knowledge_base_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A knowledge base's applied policy rides on its own search plugin, so this one speaks only the floor."""
    stopped = await _open_stopped(monkeypatch, knowledge_bases=_two_knowledge_bases())

    assert stopped.probe.captured_guidance
    assert stopped.probe.guidance is not None
    assert stopped.probe.guidance() == knowledge_base_guidance(None, KNOWLEDGE_BASE_GUIDANCE)()


async def test_an_empty_registry_leaves_the_door_saying_it_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with nothing registered still opens the plugin, and the plugin says so plainly."""
    stopped = await _open_stopped(monkeypatch, knowledge_bases=())

    assert stopped.probe.spec is None
    assert stopped.probe.grounding == ()
    assert stopped.probe.domain is not None
    assert "No knowledge base is registered" in stopped.probe.domain


async def test_a_lone_auto_mode_knowledge_base_still_opens_a_bare_anonymous_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry resolving to one auto-mode base must not turn this plugin into that base.

    The plugin is opened with `spec=None`, so identity and the `Earned` wrap are decided by what the
    plugin owns rather than by how many specs the render is grounded in. Were that keyed on the
    count instead, this deployment would hand the plugin that knowledge base's collection, wrap it in
    `Earned`, and fail every SQL-plugin call on the fixed-mode guard. Opened for real here, with
    only the two steps that need a live connection stubbed, so the outcome is the assertion.
    """

    async def _skip_registration(engine: object) -> None:
        del engine

    auto = research_spec().model_copy(update={"retrieval": RetrievalSpec(mode="auto", collection="solo")})
    monkeypatch.setattr(embedded, "verify_reader_role", _skip_registration)
    monkeypatch.setattr(embedded, "register_pgvector", _skip_registration)

    async with knowledge_base_capability(
        FakeStudioApi(),
        gateway=FakeDomainReaderGateway,
        knowledge_bases=_reads((_knowledge_base("solo", auto),)),
        reader_dsn=lambda: _READER_DSN,
        reading=KnowledgeBaseReading(guidance=KNOWLEDGE_BASE_GUIDANCE),
    ) as plugin:
        assert isinstance(plugin, SqlKnowledgeBase)
        assert isinstance(plugin.gateway, ReadOnlySqlGateway)
        assert plugin.gateway.collection is None, "the plugin speaks for this base, it is not this base"


async def test_knowledge_base_domain_reads_the_collections_own_indexed_body() -> None:
    gateway = FakeDomainReaderGateway(bodies={"research": "A knowledge base about retrieval and agent memory."})

    domain = await _knowledge_base_domain(gateway, collection="research")

    assert domain == "A knowledge base about retrieval and agent memory."


async def test_knowledge_base_domain_speaks_plainly_with_no_indexed_row() -> None:
    """No borrowed prose: a knowledge base with nothing indexed yet says so, in its own name."""
    gateway = FakeDomainReaderGateway()

    domain = await _knowledge_base_domain(gateway, collection="field-notes")

    assert "field-notes" in domain
    assert "not indexed" in domain
