"""The wrapper's own behavior: projection on write, tombstone on delete, derived index, teaching refusals."""

from collections.abc import Mapping

import pytest

from pydantic_ai_harness.memory import InMemoryStore, MemoryOperation, MemorySearchMatch

from engram.agent_memory.contracts import EngramMemoryStore, search_categories
from engram.agent_memory.domain import path_of
from engram.testing.fakes.agent_memory import FakeCards, FakeIngest, FakeSearchHits

SCOPE = "global/main"
PATH = f"{SCOPE}/world/postgres-port.md"
NOTE = "# Local Postgres port\nAnswers on 5433.\n"


def store(
    cards: FakeCards | None = None,
    ingest: FakeIngest | None = None,
    search_hits: FakeSearchHits | None = None,
) -> tuple[EngramMemoryStore, FakeIngest]:
    fake_ingest = ingest or FakeIngest()
    return EngramMemoryStore(InMemoryStore(), fake_ingest, cards or FakeCards(), search_hits=search_hits), fake_ingest


def _frontmatter(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Narrow a recorded payload's `frontmatter` value (typed `object`) to a mapping."""
    value = payload["frontmatter"]
    assert isinstance(value, Mapping)
    return value


async def test_write_stores_and_projects() -> None:
    memory, ingest = store()
    mutation = await memory.write(PATH, NOTE, expected_version=None)
    assert mutation.version is not None
    (payload, collection, embed) = ingest.calls[0]
    assert collection == "agent-memory"
    assert embed is True
    frontmatter = _frontmatter(payload)
    assert frontmatter["id"] == "mem.global.main.world.postgres-port"
    assert frontmatter["status"] == "live"
    read_back = await memory.read(PATH, max_chars=10_000)
    assert read_back is not None
    assert read_back.content == NOTE


async def test_delete_tombstones_the_projection() -> None:
    memory, ingest = store()
    written = await memory.write(PATH, NOTE, expected_version=None)
    await memory.delete(PATH, expected_version=written.version)
    assert _frontmatter(ingest.calls[-1][0])["status"] == "retired"
    assert await memory.read(PATH, max_chars=100) is None


async def test_index_reads_derived_and_refuses_writes() -> None:
    cards = FakeCards([("mem.global.main.world.postgres-port", "Local Postgres port", "Answers on 5433.")])
    memory, _ = store(cards=cards)
    index = await memory.read(f"{SCOPE}/MEMORY.md", max_chars=10_000)
    assert index is not None
    assert "- [Local Postgres port](global/main/world/postgres-port.md): Answers on 5433." in index.content
    with pytest.raises(ValueError, match="indexed"):
        await memory.write(f"{SCOPE}/MEMORY.md", "- a bullet", expected_version=None)


async def test_cards_answers_the_same_rows_the_derived_index_reads() -> None:
    cards = FakeCards([("mem.global.main.world.postgres-port", "Local Postgres port", "Answers on 5433.")])
    memory, _ = store(cards=cards)
    assert await memory.cards() == (("mem.global.main.world.postgres-port", "Local Postgres port", "Answers on 5433."),)


async def test_index_lists_only_this_scope() -> None:
    cards = FakeCards(
        [
            ("mem.global.main.world.postgres-port", "Local Postgres port", "Answers on 5433."),
            ("mem.project-x.main.project.build-order", "Build order", "Engram first."),
        ]
    )
    memory, _ = store(cards=cards)
    index = await memory.read(f"{SCOPE}/MEMORY.md", max_chars=10_000)
    assert index is not None
    assert "Build order" not in index.content


async def test_bad_category_teaches_and_stores_nothing() -> None:
    memory, ingest = store()
    with pytest.raises(ValueError, match="world, project, reference"):
        await memory.write(f"{SCOPE}/gossip/x.md", "# x\nx\n", expected_version=None)
    assert ingest.calls == []
    assert await memory.read(f"{SCOPE}/gossip/x.md", max_chars=100) is None


async def test_projection_failure_still_raises_but_authority_holds() -> None:
    ingest = FakeIngest()
    ingest.fail_next = True
    memory, _ = store(ingest=ingest)
    with pytest.raises(RuntimeError, match="projection unavailable"):
        await memory.write(PATH, NOTE, expected_version=None)
    stored = await memory.read(PATH, max_chars=100)
    assert stored is not None


async def test_search_without_hits_falls_back_to_inner_lexical_in_every_category_folder() -> None:
    memory, _ = store()
    await memory.write(PATH, NOTE, expected_version=None)
    result = await memory.search(SCOPE + "/", "postgres", limit=5, max_files=100, max_chars=2_000, max_file_chars=8_000)
    assert result.matches
    assert result.matches[0].path.endswith("postgres-port.md")


async def test_a_search_by_category_ranks_every_folder_together_within_one_searchs_bounds() -> None:
    inner = InMemoryStore()
    await inner.write(f"{SCOPE}/world/postgres-port.md", "postgres answers on 5433", expected_version=None)
    await inner.write(f"{SCOPE}/reference/postgres-tuning.md", "postgres postgres postgres tuning notes", expected_version=None)

    result = await search_categories(inner, SCOPE + "/", "postgres", limit=1, max_files=20, max_chars=50, max_file_chars=8_000)

    assert [match.path for match in result.matches] == [f"{SCOPE}/reference/postgres-tuning.md"]
    assert len(result.matches[0].path) + len(result.matches[0].snippet) <= 50
    assert result.truncated is True
    assert result.scanned == 2


async def test_search_with_wired_hits_passes_the_prefix_through_and_maps_matches() -> None:
    hits = FakeSearchHits([("mem.global.main.world.postgres-port", "Answers on 5433.", 0.9)])
    memory, _ = store(search_hits=hits)

    result = await memory.search(SCOPE + "/", "postgres", limit=5, max_files=20, max_chars=2_000, max_file_chars=8_000)

    assert hits.calls == [("postgres", SCOPE + "/", 20)]
    assert result.matches == [MemorySearchMatch(path=PATH, snippet="Answers on 5433.", score=0.9)]
    assert result.scanned == 1
    assert result.truncated is False


async def test_search_truncates_when_more_hits_come_back_than_the_limit() -> None:
    hits = FakeSearchHits(
        [
            ("mem.global.main.world.a", "First.", 0.9),
            ("mem.global.main.world.b", "Second.", 0.8),
            ("mem.global.main.world.c", "Third.", 0.7),
        ]
    )
    memory, _ = store(search_hits=hits)

    result = await memory.search(SCOPE + "/", "postgres", limit=2, max_files=20, max_chars=2_000, max_file_chars=8_000)

    assert len(result.matches) == 2
    assert result.scanned == 3
    assert result.truncated is True


async def test_search_truncates_on_the_max_chars_budget_even_within_the_limit() -> None:
    hits = FakeSearchHits(
        [
            ("mem.global.main.world.a", "First.", 0.9),
            ("mem.global.main.world.b", "Second.", 0.8),
        ]
    )
    memory, _ = store(search_hits=hits)
    first_path = path_of("mem.global.main.world.a")
    # Exactly the first match's own path + snippet: nothing is left for the second.
    max_chars = len(first_path) + len("First.")

    result = await memory.search(SCOPE + "/", "postgres", limit=5, max_files=20, max_chars=max_chars, max_file_chars=8_000)

    assert len(result.matches) == 1
    assert result.matches[0].path == first_path
    assert result.truncated is True


async def test_search_shares_a_shrinking_budget_across_matches() -> None:
    first_id, first_snippet = "mem.global.main.world.a", "First note."
    second_id, second_snippet = "mem.global.main.world.b", "Second note that is much longer than what remains."
    hits = FakeSearchHits([(first_id, first_snippet, 0.9), (second_id, second_snippet, 0.8)])
    memory, _ = store(search_hits=hits)
    first_path, second_path = path_of(first_id), path_of(second_id)
    budget_left_for_second = 10
    max_chars = len(first_path) + len(first_snippet) + len(second_path) + budget_left_for_second

    result = await memory.search(SCOPE + "/", "postgres", limit=5, max_files=20, max_chars=max_chars, max_file_chars=8_000)

    assert len(result.matches) == 2
    assert result.matches[0].snippet == first_snippet
    assert result.matches[1].snippet == second_snippet[:budget_left_for_second]
    assert result.matches[1].snippet != second_snippet
    # Both matches survived, and no snippet was dropped entirely: the shared budget shrank
    # match-to-match rather than starving anything, so this is not a truncated result.
    assert result.truncated is False


async def test_search_keeps_a_hit_with_an_empty_snippet_and_the_hits_after_it() -> None:
    hits = FakeSearchHits(
        [
            ("mem.global.main.world.a", "First.", 0.9),
            ("mem.global.main.world.b", "", 0.8),
            ("mem.global.main.world.c", "Third.", 0.7),
        ]
    )
    memory, _ = store(search_hits=hits)

    result = await memory.search(SCOPE + "/", "postgres", limit=5, max_files=20, max_chars=2_000, max_file_chars=8_000)

    assert result.matches == [
        MemorySearchMatch(path=path_of("mem.global.main.world.a"), snippet="First.", score=0.9),
        MemorySearchMatch(path=path_of("mem.global.main.world.b"), snippet="", score=0.8),
        MemorySearchMatch(path=path_of("mem.global.main.world.c"), snippet="Third.", score=0.7),
    ]
    assert result.truncated is False


async def test_receipts_replay_through_the_wrapper() -> None:
    memory, ingest = store()
    operation = MemoryOperation(id="op-1", fingerprint="fp-1")
    first = await memory.write(PATH, NOTE, expected_version=None, operation=operation)
    replay = await memory.write(PATH, NOTE, expected_version=None, operation=operation)
    assert replay.replayed
    assert replay.version == first.version
    assert len(ingest.calls) == 1
