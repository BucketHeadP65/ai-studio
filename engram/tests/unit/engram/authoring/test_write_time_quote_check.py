"""The quote check `NoteWriter` runs at write time against the mirror a page names, and the locator swap before it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools, NoteRefusedError, NoteWriter
from engram.authoring.infrastructure.mirrors import mirror_text
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

_SENTENCE = "a good code LM still ought to reason about execution"

_FACETS = """---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facets"
summary: The claim types and paper types these fixtures use.
status: verified
---

## `kind` on claim blocks (single value)

| Value | Meaning |
|---|---|
| `author-claim` | The authors say so |
| `observation` | Something observed |

## `paper_type` on papers (single value)

| Value | Meaning |
|---|---|
| `benchmark` | A benchmark |
"""

_MIRROR = f"""---
source_key: arxiv-1
provider: intake
title: A benchmark
---

# A benchmark

We think {_SENTENCE}, the way a strong programmer does.
"""


def _spec(*, quote_verify: bool = True) -> KnowledgeBaseSchema:
    spec = research_spec()
    return spec.model_copy(update={"sources": spec.sources.model_copy(update={"quote_verify": quote_verify})})


def _knowledge_base(tmp_path: Path, *, quote_verify: bool = True, mirror: str = _MIRROR, mirror_path: str = "Sources/a-benchmark.md") -> AuthoringKnowledgeBase:
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_FACETS, encoding="utf-8")
    target = tmp_path / mirror_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(mirror, encoding="utf-8")
    return AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=_spec(quote_verify=quote_verify))


def _paper(claims: list[dict[str, object]], *, key: str = "arxiv-1", sections: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "frontmatter": {
            "id": "paper.a-benchmark",
            "type": "paper",
            "title": "A benchmark",
            "summary": "A benchmark of code reasoning.",
            "status": "active",
            "paper_type": "benchmark",
            "year": 2024,
            "zotero_key": key,
            "source_hash": "h1",
            "digest_of": key,
        },
        "sections": sections if sections is not None else [{"heading": "TL;DR", "body": "A benchmark."}],
        "claims": claims,
    }


def test_a_locator_quote_is_swapped_with_its_loc_and_named_in_the_warnings(tmp_path: Path) -> None:
    written = NoteWriter(_knowledge_base(tmp_path)).write(_paper([{"text": "LMs should reason.", "kind": "author-claim", "quote": "§7", "loc": _SENTENCE}]))

    text = written.path.read_text(encoding="utf-8")
    assert "    - loc:: §7" in text
    assert f'    - quote:: "{_SENTENCE}"' in text
    assert any(warning.startswith("claim ^c1: its quote held the locator '§7'") for warning in written.warnings)


def test_a_quote_the_mirror_lacks_is_refused_naming_the_claim_and_nothing_is_written(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    claims: list[dict[str, object]] = [
        {"text": "Found.", "kind": "observation", "quote": _SENTENCE, "loc": "§1"},
        {"text": "Invented.", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"},
    ]

    with pytest.raises(NoteRefusedError, match=r"claim \^c2: claim quote not found in the source mirror \('words nobody wrote'\)\. Copy the quote word for word"):
        NoteWriter(knowledge_base).write(_paper(claims))
    assert not (tmp_path / "Wiki" / "Papers").exists()


def test_a_quote_found_in_the_mirror_writes_with_no_swap(tmp_path: Path) -> None:
    written = NoteWriter(_knowledge_base(tmp_path)).write(_paper([{"text": "x", "kind": "observation", "quote": _SENTENCE, "loc": "§1"}]))
    assert not any("swapped" in warning for warning in written.warnings)


def test_a_claim_whose_quote_and_loc_are_both_locators_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NoteRefusedError, match=r"claim \^c1: claim quote not found"):
        NoteWriter(_knowledge_base(tmp_path)).write(_paper([{"text": "x", "kind": "observation", "quote": "§7", "loc": "§7"}]))


def test_claims_written_in_a_section_body_are_checked_and_never_swapped(tmp_path: Path) -> None:
    body = f'- LMs should reason. ^c1\n    - loc:: {_SENTENCE}\n    - quote:: "§7"\n    - kind:: author-claim'
    payload = _paper([], sections=[{"heading": "TL;DR", "body": "A benchmark."}, {"heading": "Findings", "body": body}])

    with pytest.raises(NoteRefusedError, match=r"claim \^c1: claim quote not found"):
        NoteWriter(_knowledge_base(tmp_path)).write(payload)


def _article(claims: list[dict[str, object]]) -> dict[str, object]:
    """An article digest, which names its mirror only through the digest link field."""
    return {
        "frontmatter": {
            "id": "art.a-benchmark",
            "type": "article",
            "title": "A benchmark",
            "summary": "A benchmark of code reasoning.",
            "status": "active",
            "digest_of": "arxiv-1",
        },
        "sections": [{"heading": "TL;DR", "body": "A benchmark."}],
        "claims": claims,
    }


def test_a_page_naming_its_mirror_only_through_the_digest_link_field_is_checked(tmp_path: Path) -> None:
    payload = _article([{"text": "Invented.", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}])

    with pytest.raises(NoteRefusedError, match=r"claim \^c1: claim quote not found in the source mirror"):
        NoteWriter(_knowledge_base(tmp_path)).write(payload)


def test_a_page_naming_no_mirror_is_written_unchecked(tmp_path: Path) -> None:
    payload = _paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}], key="arxiv-missing")
    assert NoteWriter(_knowledge_base(tmp_path)).write(payload).path.is_file()


def test_a_knowledge_base_that_does_not_verify_quotes_is_never_checked(tmp_path: Path) -> None:
    written = NoteWriter(_knowledge_base(tmp_path, quote_verify=False)).write(_paper([{"text": "x", "kind": "observation", "quote": "§7", "loc": _SENTENCE}]))
    assert '    - quote:: "§7"' in written.path.read_text(encoding="utf-8"), "no check, no swap"


def test_a_mirror_is_found_by_its_file_stem(tmp_path: Path) -> None:
    unkeyed = f"---\nprovider: intake\ntitle: T\n---\n\nWe think {_SENTENCE}.\n"
    knowledge_base = _knowledge_base(tmp_path, mirror=unkeyed, mirror_path="Sources/arxiv-2.md")
    with pytest.raises(NoteRefusedError, match="claim quote not found"):
        NoteWriter(knowledge_base).write(_paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}], key="arxiv-2"))


@pytest.mark.parametrize("mirror_path", ["Sources/papers/a-benchmark.md", "Sources/_archive/arxiv-1.md"])
def test_a_mirror_in_a_folder_under_sources_is_not_read(tmp_path: Path, mirror_path: str) -> None:
    knowledge_base = _knowledge_base(tmp_path, mirror_path=mirror_path)
    payload = _paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}])
    assert NoteWriter(knowledge_base).write(payload).path.is_file(), "the index reads Sources/*.md only, so nothing is checked"


def test_a_page_citing_a_provider_less_mirror_is_written_unchecked(tmp_path: Path) -> None:
    providerless = _MIRROR.replace("provider: intake\n", "")
    knowledge_base = _knowledge_base(tmp_path, mirror=providerless)
    payload = _paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}])
    assert NoteWriter(knowledge_base).write(payload).path.is_file(), "the index drops a mirror with no provider, so nothing is checked"


def test_an_unreadable_file_in_sources_does_not_stop_the_check(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (tmp_path / "Sources" / "0-broken.md").write_bytes(b"---\nsource_key: arxiv-1\n\xff\xfe\n---\n")
    payload = _paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}])
    with pytest.raises(NoteRefusedError, match="claim quote not found"):
        NoteWriter(knowledge_base).write(payload)
    good = _paper([{"text": "x", "kind": "observation", "quote": _SENTENCE, "loc": "§2"}])
    assert NoteWriter(knowledge_base).write(good).path.is_file()


def test_check_answers_the_same_swap_and_writes_nothing(tmp_path: Path) -> None:
    checked = NoteWriter(_knowledge_base(tmp_path)).check(_paper([{"text": "x", "kind": "author-claim", "quote": "§7", "loc": _SENTENCE}]))
    assert any("swapped" in warning for warning in checked.warnings)
    assert not checked.path.exists()


def test_mirror_text_answers_the_whole_file_of_the_mirror_whose_key_matches(tmp_path: Path) -> None:
    _knowledge_base(tmp_path)
    found = mirror_text(tmp_path, "arxiv-1")
    assert found is not None
    assert found.startswith("---\nsource_key: arxiv-1")
    assert mirror_text(tmp_path, "arxiv-9") is None
    assert mirror_text(tmp_path, "") is None


async def test_write_note_answers_the_swap_and_refuses_a_missing_quote_as_a_retry(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    doors = AuthoringTools(
        knowledge_bases={"research": knowledge_base},
        reindex=FakeReindex(),
        admin=FakeAdminGateway(rows=[{"source_id": "arxiv-1", "provider": "intake", "title": "A benchmark"}]),
        queue_for=lambda _target: pytest.fail("no source queue is read"),
    )

    said = await doors.write_note("research", _paper([{"text": "x", "kind": "author-claim", "quote": "§7", "loc": _SENTENCE}]))
    assert "(warnings: claim ^c1: its quote held the locator '§7'" in said

    with pytest.raises(ModelRetry, match=r"claim \^c1: claim quote not found"):
        await doors.write_note("research", _paper([{"text": "x", "kind": "observation", "quote": "words nobody wrote", "loc": "§2"}]))
