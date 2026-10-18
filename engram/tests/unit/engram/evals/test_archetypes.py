"""Unit specs for the archetype vocabulary (`engram.evals.generation.service.archetypes`).

The vocabulary maps each `Archetype` to an `ArchetypeSpec` pairing the matching `SeedQueries`
seeder with a per-archetype drafting instruction fragment. These specs pin the contract: every
offered name resolves, the vocabulary covers exactly the enum, an unknown name is rejected rather
than silently defaulting, each spec's instructions are non-empty, and each spec's `seed` dispatches
to the matching repository method. The idiom mirrors `engram.evals.tuning.strategy`'s vocabulary tests.
"""

from __future__ import annotations

import pytest

from engram.evals.generation.contracts import ARCHETYPE_NAMES, resolve_archetype
from engram.evals.generation.service.archetypes import ArchetypeSpec
from engram.evals.shared.domain import Archetype, QaDifficulty, Seed


class RecordingSeedQueries:
    """A seeder stand-in that records which seeder method an `ArchetypeSpec.seed` dispatches to.

    Each seeder records its name and returns an empty list, so a test can assert the spec routes to the
    matching repository method without touching a database. Structurally satisfies the slice of
    `SeedQueries` the archetype seeders call.
    """

    def __init__(self) -> None:
        self.called: list[str] = []

    async def pinpoint_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        self.called.append("pinpoint_seeds")
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []

    async def survey_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        self.called.append("survey_seeds")
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []

    async def trace_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        self.called.append("trace_seeds")
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []

    async def synthesis_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        self.called.append("synthesis_seeds")
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []

    async def passage_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        self.called.append("passage_seeds")
        del collection, page_ids, limit, salt, band, anchor_page_type
        return []


class TestArchetypeNames:
    def test_every_offered_name_resolves_to_a_spec(self):
        for name in ARCHETYPE_NAMES:
            spec = resolve_archetype(name)
            assert spec.archetype.value == name  # the spec's archetype matches the name it resolved from
            assert spec.instructions  # every archetype carries a non-empty drafting fragment

    def test_the_default_is_listed_first(self):
        assert ARCHETYPE_NAMES[0] == Archetype.PINPOINT.value

    def test_names_match_the_enum(self):
        assert set(ARCHETYPE_NAMES) == {a.value for a in Archetype}
        assert len(ARCHETYPE_NAMES) == len(Archetype)  # no duplicates


class TestResolveArchetype:
    @pytest.mark.parametrize("archetype", list(Archetype))
    def test_each_archetype_resolves_to_its_own_spec(self, archetype: Archetype):
        spec = resolve_archetype(archetype.value)

        assert isinstance(spec, ArchetypeSpec)
        assert spec.archetype is archetype

    def test_an_unknown_archetype_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown archetype") as excinfo:
            resolve_archetype("bogus")

        # the error names the offending value and the legal set, so the caller can fix the contract
        message = str(excinfo.value)
        assert "bogus" in message
        for name in ARCHETYPE_NAMES:
            assert name in message

    @pytest.mark.parametrize("archetype", list(Archetype))
    def test_every_spec_has_nonempty_instructions(self, archetype: Archetype):
        spec = resolve_archetype(archetype.value)

        assert isinstance(spec.instructions, str)
        assert spec.instructions.strip()  # not just whitespace


class TestSeedDispatch:
    """Each spec's `seed` routes to the matching `SeedQueries` method (one-to-one with the archetype)."""

    @pytest.mark.parametrize(
        ("archetype", "method"),
        [
            (Archetype.PINPOINT, "pinpoint_seeds"),
            (Archetype.SURVEY, "survey_seeds"),
            (Archetype.TRACE, "trace_seeds"),
            (Archetype.SYNTHESIS, "synthesis_seeds"),
            (Archetype.PASSAGE, "passage_seeds"),
        ],
    )
    async def test_seed_dispatches_to_the_matching_repository_method(self, archetype: Archetype, method: str):
        spec = resolve_archetype(archetype.value)
        repo = RecordingSeedQueries()

        result = await spec.seed(repo, collection="research", page_ids=(), limit=5)

        assert repo.called == [method]  # routed to exactly the matching seeder, nothing else
        assert result == []
