"""`AuthoringTools.skip_digest`: every digest page of one source set to skipped, with the reason on each, or nothing changed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.authoring.contracts import AuthoringKnowledgeBase, NoteWriter
from engram.testing.fakes.authoring import FakeAdminGateway
from engram.testing.specs import research_spec
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

_LEAD = "talk.agents-that-plan"
_SECTION = "talk.agents-that-plan.methods"


def _talk(page_id: str) -> dict[str, object]:
    return {
        "frontmatter": {"id": page_id, "type": "talk", "title": "Agents that plan", "summary": "A talk on planning agents.", "status": "active", "digest_of": "yt-abc"},
        "sections": [{"heading": "TL;DR", "body": "Planning agents keep a task list."}, {"heading": "Findings", "body": ""}, {"heading": "Entities", "body": ""}],
    }


def _write_both(tmp_path: Path, spec: KnowledgeBaseSchema) -> None:
    writer = NoteWriter(AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=spec))
    writer.write(_talk(_LEAD))
    writer.write(_talk(_SECTION))


def _gateway() -> FakeAdminGateway:
    return FakeAdminGateway(
        rows_queue=[
            [{"id": _SECTION, "source_id": "yt-abc"}],
            [{"id": _LEAD, "page_type": "talk"}, {"id": _SECTION, "page_type": "talk"}],
        ]
    )


async def test_every_digest_page_of_the_source_is_skipped_with_the_reason_and_indexed_once(tmp_path: Path) -> None:
    admin = _gateway()
    doors, reindex, _ = _doors(tmp_path, admin)
    _write_both(tmp_path, research_spec())

    said = await doors.skip_digest("research", _SECTION, "Skipped: no finding bears on the purpose.")

    assert said.startswith(f"2 digest pages of yt-abc are now skipped in research: {_LEAD}, {_SECTION}, reason noted, indexed clean")
    for stem in ("agents-that-plan", "agents-that-plan.methods"):
        text = (tmp_path / "Wiki" / "Talks" / f"{stem}.md").read_text(encoding="utf-8")
        assert "status: skipped" in text
        assert "decision_reason: 'Skipped: no finding bears on the purpose.'" in text
    assert len(reindex.knowledge_bases) == 1
    lookup_sql, lookup_params = admin.calls[0]
    assert "p.frontmatter->>'digest_of'" in lookup_sql, "the source comes through the schema's own link field"
    assert lookup_params == {"collection": "research", "page_id": _SECTION}
    assert admin.calls[1][1] == {"collection": "research", "source_id": "yt-abc"}


async def test_a_page_that_names_no_source_is_refused_as_not_a_digest(tmp_path: Path) -> None:
    doors, reindex, _ = _doors(tmp_path, FakeAdminGateway(rows_queue=[[{"id": "con.x", "source_id": ""}]]))
    with pytest.raises(ModelRetry, match=r"con\.x names no source through digest_of, so it is not a digest"):
        await doors.skip_digest("research", "con.x", "Skipped: off purpose.")
    assert reindex.knowledge_bases == []


async def test_an_unknown_page_is_refused(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows_queue=[[]]))
    with pytest.raises(ModelRetry, match=r"no page 'talk\.nope' in 'research'"):
        await doors.skip_digest("research", "talk.nope", "Skipped: off purpose.")


async def test_an_empty_reason_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin)
    with pytest.raises(ModelRetry, match="reason"):
        await doors.skip_digest("research", _SECTION, "  ")
    assert admin.calls == []


async def test_a_page_type_with_no_skipped_status_changes_nothing(tmp_path: Path) -> None:
    base = research_spec()
    page_types = tuple(pt.model_copy(update={"status_values": ("active", "outdated")}) if pt.page_type == "talk" else pt for pt in base.page_types)
    spec = base.model_copy(update={"page_types": page_types})
    doors, reindex, _ = _doors(tmp_path, _gateway(), spec=spec)
    _write_both(tmp_path, spec)

    with pytest.raises(ModelRetry, match=r"talk\.agents-that-plan is a talk page, whose statuses are \['active', 'outdated'\], none of them skipped"):
        await doors.skip_digest("research", _SECTION, "Skipped: off purpose.")

    assert "status: active" in (tmp_path / "Wiki" / "Talks" / "agents-that-plan.methods.md").read_text(encoding="utf-8")
    assert reindex.knowledge_bases == []


def test_skip_digest_stays_out_of_the_twenty(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)
    names = [name for _call, name, _description in doors.tools()]
    assert len(names) == 20
    assert "skip_digest" not in names
