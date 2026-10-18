"""The archetype registry: the seed draw and the drafting prompt each archetype resolves to.

Each `Archetype` resolves to a `seed` call that draws its structural `Seed`s and to the `instructions`
its drafter writes question, answer and quotes under. `resolve_archetype` refuses an unknown name and
never falls back to a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engram.evals.shared.contracts import Archetype

if TYPE_CHECKING:
    from engram.evals.generation.internal_contracts import SeedFn, SupportsSeeding
    from engram.evals.shared.contracts import QaDifficulty, Seed


@dataclass(frozen=True)
class ArchetypeSpec:
    """What an archetype resolves to: its enum, the seeder it dispatches to, and its drafting fragment."""

    archetype: Archetype
    seed: SeedFn
    instructions: str


# One seeder per archetype, each passing its arguments unchanged to the matching draw on `repo`.
async def _pinpoint(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
    repo: SupportsSeeding,
    *,
    collection: str,
    page_ids: tuple[str, ...],
    limit: int,
    salt: str = "",
    band: QaDifficulty | None = None,
    anchor_page_type: str | None = None,
) -> list[Seed]:
    """Draw `pinpoint` seeds through `repo.pinpoint_seeds`."""
    return await repo.pinpoint_seeds(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


async def _survey(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
    repo: SupportsSeeding,
    *,
    collection: str,
    page_ids: tuple[str, ...],
    limit: int,
    salt: str = "",
    band: QaDifficulty | None = None,
    anchor_page_type: str | None = None,
) -> list[Seed]:
    """Draw `survey` seeds through `repo.survey_seeds`."""
    return await repo.survey_seeds(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


async def _trace(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
    repo: SupportsSeeding,
    *,
    collection: str,
    page_ids: tuple[str, ...],
    limit: int,
    salt: str = "",
    band: QaDifficulty | None = None,
    anchor_page_type: str | None = None,
) -> list[Seed]:
    """Draw `trace` seeds through `repo.trace_seeds`."""
    return await repo.trace_seeds(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


async def _synthesis(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
    repo: SupportsSeeding,
    *,
    collection: str,
    page_ids: tuple[str, ...],
    limit: int,
    salt: str = "",
    band: QaDifficulty | None = None,
    anchor_page_type: str | None = None,
) -> list[Seed]:
    """Draw `synthesis` seeds through `repo.synthesis_seeds`."""
    return await repo.synthesis_seeds(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


async def _passage(  # noqa: PLR0913 (each stratified-draw input is distinct, and bundling them would obscure the seeder contract)
    repo: SupportsSeeding,
    *,
    collection: str,
    page_ids: tuple[str, ...],
    limit: int,
    salt: str = "",
    band: QaDifficulty | None = None,
    anchor_page_type: str | None = None,
) -> list[Seed]:
    """Draw `passage` seeds through `repo.passage_seeds`."""
    return await repo.passage_seeds(collection=collection, page_ids=page_ids, limit=limit, salt=salt, band=band, anchor_page_type=anchor_page_type)


# One `ArchetypeSpec` per `Archetype` member.
_ARCHETYPES: dict[Archetype, ArchetypeSpec] = {
    Archetype.PINPOINT: ArchetypeSpec(
        archetype=Archetype.PINPOINT,
        seed=_pinpoint,
        instructions=(
            "Write one specific factual question answerable from a single source's exact claim or quote. "
            "It must pin down one fact, not invite a survey or comparison."
        ),
    ),
    Archetype.SURVEY: ArchetypeSpec(
        archetype=Archetype.SURVEY,
        seed=_survey,
        instructions=(
            "Write a breadth question requiring aggregation across many sources that share a relation. "
            "The answer must gather evidence from the whole set, not lean on any single source."
        ),
    ),
    Archetype.TRACE: ArchetypeSpec(
        archetype=Archetype.TRACE,
        seed=_trace,
        instructions=(
            "Write a question that follows a multi-hop chain of relations from one source to another. "
            "Answering it must require traversing the chain step by step, not a single lookup."
        ),
    ),
    Archetype.SYNTHESIS: ArchetypeSpec(
        archetype=Archetype.SYNTHESIS,
        seed=_synthesis,
        instructions=(
            "Write an integrative question that spans multiple sources across different sections. "
            "The answer must synthesise the sources into one coherent account, not quote a single passage."
        ),
    ),
    Archetype.PASSAGE: ArchetypeSpec(
        archetype=Archetype.PASSAGE,
        seed=_passage,
        instructions=(
            "Write one specific question that this one section of one page answers. "
            "The answer must come from the section's own words, not from other sources or general knowledge."
        ),
    ),
}

_DEFAULT = Archetype.PINPOINT

ARCHETYPE_NAMES: tuple[str, ...] = (
    _DEFAULT.value,
    Archetype.SURVEY.value,
    Archetype.TRACE.value,
    Archetype.SYNTHESIS.value,
    Archetype.PASSAGE.value,
)
"""Every archetype name, the default `pinpoint` first, to validate a name that arrives as a string."""


def resolve_archetype(name: str) -> ArchetypeSpec:
    """Resolve an archetype name to its `ArchetypeSpec`, refusing an unknown name.

    Args:
        name: An `Archetype` value, such as `"survey"`. Callers pick their own default from
            `ARCHETYPE_NAMES`.

    Returns:
        The `ArchetypeSpec` pairing the matching seeder with its drafting instructions.

    Raises:
        ValueError: When `name` is not a known archetype.
    """
    try:
        archetype = Archetype(name)
    except ValueError as exc:
        msg = f"unknown archetype: {name!r} (expected one of {', '.join(ARCHETYPE_NAMES)})"
        raise ValueError(msg) from exc
    return _ARCHETYPES[archetype]
