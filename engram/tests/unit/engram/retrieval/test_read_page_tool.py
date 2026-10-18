"""`AuthoringTools.read_page`: one indexed page's path and a ready write_note payload of its file, by its id."""

from __future__ import annotations

import copy
import difflib

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.testing.fakes.authoring import FakeAdminGateway
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from pathlib import Path

    from engram.authoring.contracts import AuthoringTools

_FACETS = """---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facets"
summary: The claim kinds these fixtures use.
status: verified
---

## `kind`: on claim blocks

| Value | Meaning |
|---|---|
| `observation` | Something observed |
"""

_CLAIMS: list[dict[str, object]] = [
    {
        "text": "Planning agents keep a task list.",
        "loc": "§1",
        "quote": "agents keep a task list",
        "kind": "observation",
        "subject": "[[ent.kestrel]]",
        "condition": "long sessions",
    },
    {
        "text": "The list survives compaction.",
        "loc": "§2",
        "quote": "survives compaction",
        "kind": "observation",
        "on_task": "[[task.planning]]",
        "on_dataset": ["[[dataset.alpha]]", "[[dataset.beta]]"],
    },
    {"text": "Wren beats the baseline.", "loc": "§3", "quote": "beats the baseline", "kind": "observation", "magnitude": "12 points"},
]

_TALK: dict[str, object] = {
    "frontmatter": {
        "id": "talk.agents-that-plan",
        "type": "talk",
        "title": "Agents that plan",
        "summary": "A talk on planning agents.",
        "status": "active",
        "digest_of": "yt-abc",
        "channel": "Example Channel",
        "published": "2024-05-01",
        "alternative_to": ["[[talk.other-talk]]"],
    },
    "sections": [
        {"heading": "TL;DR", "body": "Planning agents keep a task list the model owns."},
        {"heading": "Findings", "body": ""},
        {"heading": "Entities", "body": "Kestrel and Wren."},
        {"heading": "Open questions", "body": "Whether it scales."},
        {"heading": "Later", "level": 3, "body": "Revisit next year."},
    ],
    "claims": _CLAIMS,
}

_TALK_PATH = "Wiki/Talks/agents-that-plan.md"


async def _written_talk(tmp_path: Path) -> tuple[AuthoringTools, str]:
    """Doors over a knowledge base holding `_TALK` written through write_note and indexed at `_TALK_PATH`, and the file's text."""
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "title": "Agents that plan", "path": _TALK_PATH}])
    doors, _, _ = _doors(tmp_path, admin)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_FACETS, encoding="utf-8")
    await doors.write_note("research", _TALK)
    return doors, (tmp_path / _TALK_PATH).read_text(encoding="utf-8")


async def _payload_of_talk(doors: AuthoringTools) -> dict[str, object]:
    page = await doors.read_page("research", "talk.agents-that-plan")
    assert page["path"] == _TALK_PATH
    payload = page["payload"]
    assert isinstance(payload, dict)
    copied: dict[str, object] = dict(copy.deepcopy(payload))
    return copied


async def test_read_page_answers_a_known_page_s_id_path_and_payload(tmp_path: Path) -> None:
    doors, _ = await _written_talk(tmp_path)

    page = await doors.read_page("research", "talk.agents-that-plan")

    assert page["page_id"] == "talk.agents-that-plan"
    assert page["path"] == _TALK_PATH
    payload = page["payload"]
    assert isinstance(payload, dict)
    frontmatter, claims = payload["frontmatter"], payload["claims"]
    assert isinstance(frontmatter, dict)
    assert isinstance(claims, list)
    assert frontmatter["channel"] == "Example Channel"
    assert [claim["text"] if isinstance(claim, dict) else None for claim in claims] == [claim["text"] for claim in _CLAIMS]


async def test_the_payload_written_back_unchanged_rewrites_the_page_byte_for_byte(tmp_path: Path) -> None:
    doors, before = await _written_talk(tmp_path)
    payload = await _payload_of_talk(doors)

    await doors.write_note("research", payload, _TALK_PATH)

    assert (tmp_path / _TALK_PATH).read_text(encoding="utf-8") == before


async def test_setting_link_fields_on_the_payload_changes_only_those_lines(tmp_path: Path) -> None:
    doors, before = await _written_talk(tmp_path)
    payload = await _payload_of_talk(doors)
    frontmatter, claims = payload["frontmatter"], payload["claims"]
    assert isinstance(frontmatter, dict)
    assert isinstance(claims, list)

    frontmatter["mentions"] = ["[[ent.kestrel]]", "[[ent.wren]]"]
    claims[2]["subject"] = "[[ent.wren]]"
    await doors.write_note("research", payload, _TALK_PATH)

    after = (tmp_path / _TALK_PATH).read_text(encoding="utf-8")
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    assert [line for line in diff if line.startswith("- ")] == []
    assert [line for line in diff if line.startswith("+ ")] == [
        "+ mentions:",
        "+ - '[[ent.kestrel]]'",
        "+ - '[[ent.wren]]'",
        "+     - subject:: [[ent.wren]]",
    ]


async def test_a_page_with_a_section_its_page_type_does_not_declare_in_the_middle_rewrites_byte_for_byte(tmp_path: Path) -> None:
    sections = _TALK["sections"]
    assert isinstance(sections, list)
    tl_dr = {
        "heading": "TL;DR",
        "body": "Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup.\n\n### Setting\nA small room.",
    }
    admin = FakeAdminGateway(rows=[{"source_id": "yt-abc", "provider": "intake", "title": "Agents that plan", "path": _TALK_PATH}])
    doors, _, _ = _doors(tmp_path, admin)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_FACETS, encoding="utf-8")
    await doors.write_note("research", {**_TALK, "sections": [tl_dr, *sections[1:]]})
    before = (tmp_path / _TALK_PATH).read_text(encoding="utf-8")
    assert "## Background\n\nRecorded at a meetup.\n\n### Setting\nA small room.\n\n## Findings\n" in before
    payload = await _payload_of_talk(doors)

    await doors.write_note("research", payload, _TALK_PATH)

    assert (tmp_path / _TALK_PATH).read_text(encoding="utf-8") == before


async def test_read_page_refuses_an_id_no_indexed_page_carries_naming_where_ids_come_from(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[]))

    with pytest.raises(ModelRetry, match=r"no indexed page 'paper\.nope' in knowledge base 'research'") as refused:
        await doors.read_page("research", "paper.nope")
    assert "list_pages" in str(refused.value)


async def test_read_page_refuses_a_page_whose_file_is_gone(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, FakeAdminGateway(rows=[{"path": "Wiki/Papers/gone.md"}]))

    with pytest.raises(ModelRetry, match=r"paper\.gone was indexed from Wiki/Papers/gone\.md, which no longer exists"):
        await doors.read_page("research", "paper.gone")


async def test_read_page_refuses_a_file_a_payload_cannot_carry_whole(tmp_path: Path) -> None:
    doors, before = await _written_talk(tmp_path)
    (tmp_path / _TALK_PATH).write_text(before.replace("\n## TL;DR", "\nAn opening line no section holds.\n\n## TL;DR", 1), encoding="utf-8")

    with pytest.raises(ModelRetry, match=r"talk\.agents-that-plan at Wiki/Talks/agents-that-plan\.md cannot be read as a payload"):
        await doors.read_page("research", "talk.agents-that-plan")


async def test_read_page_refuses_an_unknown_knowledge_base(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin)

    with pytest.raises(ModelRetry, match="research"):
        await doors.read_page("elsewhere", "paper.alpha")
    assert admin.calls == []
