"""`AuthoringTools.resolve_entities`: entity resolution turns each name into the id of its one entity page, creating the missing ones."""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING

import pytest

from core.markdown import split_frontmatter
from pydantic_ai import ModelRetry

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools, EntityName, EntityPages, KnownEntities, NoteWriter
from engram.init.contracts import scaffold
from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import SourceQueue
from engram.testing.fakes.authoring import FakeAdminGateway, FakeReindex
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import JsonValue

_SPEC = KnowledgeBaseSchema.model_validate(
    {
        "slug": "directory",
        "page_types": [
            {
                "page_type": "entity",
                "id_prefix": "ent.",
                "folder": "Wiki/Entities",
                "frontmatter_fields": [{"name": "entity_type", "type": "scalar", "required": True}],
                "canonical_sections": [{"slug": "tl_dr", "heading": "TL;DR"}, "details"],
            },
            {"page_type": "note", "id_prefix": "note.", "folder": "Wiki/Notes"},
        ],
        "entity_types": [{"value": "organization"}, {"value": "person"}],
        "entities": {"page_type": "entity"},
    }
)

_ENTITIES = "Wiki/Entities"


def _tools(
    tmp_path: Path, spec: KnowledgeBaseSchema = _SPEC, entity_pages_for: Callable[[AuthoringKnowledgeBase], EntityPages] | None = None
) -> tuple[AuthoringTools, FakeReindex, AuthoringKnowledgeBase]:
    vocabulary = tmp_path / "_meta" / "vocabulary"
    vocabulary.mkdir(parents=True)
    (vocabulary / "entity-types.md").write_text(scaffold(spec)["_meta/vocabulary/entity-types.md"], encoding="utf-8")
    knowledge_base = AuthoringKnowledgeBase(name="directory", collection="directory", path=tmp_path, schema=spec)
    reindex = FakeReindex()
    tools = AuthoringTools(
        knowledge_bases={"directory": knowledge_base},
        reindex=reindex,
        admin=FakeAdminGateway(),
        queue_for=lambda _kb: SourceQueue(tmp_path / "_meta" / "source-queue.yaml", connectors=()),
        entity_pages_for=entity_pages_for,
    )
    return tools, reindex, knowledge_base


def _entity_page(page_id: str, title: str, aliases: list[str], entity_type: str = "organization") -> dict[str, object]:
    return {
        "frontmatter": {"id": page_id, "type": "entity", "title": title, "summary": "", "status": "active", "entity_type": entity_type, "aliases": aliases},
        "sections": [{"heading": "TL;DR", "body": f"{title} as another writer wrote it."}],
    }


def _rows(said: str) -> list[dict[str, object]]:
    return json.loads(said)["entities"]


def _entity_files(tmp_path: Path) -> list[str]:
    folder = tmp_path / _ENTITIES
    return sorted(path.name for path in folder.glob("*.md")) if folder.is_dir() else []


def _every_entity_file(tmp_path: Path) -> list[str]:
    folder = tmp_path / _ENTITIES
    return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*.md"))


def _lock_files(tmp_path: Path) -> list[str]:
    return sorted(path.name for path in (tmp_path / _ENTITIES).iterdir() if path.name.endswith(".lock"))


def _frontmatter(tmp_path: Path, stem: str) -> dict[str, JsonValue]:
    frontmatter, _body = split_frontmatter((tmp_path / _ENTITIES / f"{stem}.md").read_text(encoding="utf-8"))
    return frontmatter


async def test_a_name_matches_an_existing_entity_by_its_title_or_alias_whatever_the_case_punctuation_or_leading_the(tmp_path: Path) -> None:
    tools, reindex, knowledge_base = _tools(tmp_path)
    NoteWriter(knowledge_base).write(_entity_page("ent.the-kestrel-group", "The Kestrel Group", ["KG Holdings"]))

    said = await tools.resolve_entities(
        "directory",
        [
            EntityName(name="The Kestrel Group", entity_type="organization"),
            EntityName(name="kestrel group", entity_type="organization", aliases=["KG Holdings."]),
            EntityName(name="Kestrel Group.", entity_type="organization"),
            EntityName(name="kg-holdings", entity_type="organization"),
        ],
    )

    assert _rows(said) == [
        {"name": "The Kestrel Group", "id": "ent.the-kestrel-group", "created": False},
        {"name": "kestrel group", "id": "ent.the-kestrel-group", "created": False},
        {"name": "Kestrel Group.", "id": "ent.the-kestrel-group", "created": False},
        {"name": "kg-holdings", "id": "ent.the-kestrel-group", "created": False},
    ]
    assert _entity_files(tmp_path) == ["the-kestrel-group.md"]
    assert _frontmatter(tmp_path, "the-kestrel-group")["aliases"] == ["ent.the-kestrel-group", "KG Holdings"]
    assert _lock_files(tmp_path) == [], "no name brought anything new, so no page was locked"
    assert reindex.knowledge_bases == [], "nothing was written, so nothing is indexed again"


async def test_a_matched_name_adds_its_new_names_to_the_matched_page_including_names_earlier_in_the_call(tmp_path: Path) -> None:
    tools, reindex, knowledge_base = _tools(tmp_path)
    writer = NoteWriter(knowledge_base)
    writer.write(_entity_page("ent.the-kestrel-group", "The Kestrel Group", ["KG Holdings"]))
    writer.write(_entity_page("ent.harbor-works", "Harbor Works", ["HW"]))

    said = await tools.resolve_entities(
        "directory",
        [
            EntityName(name="Kestrel Group.", entity_type="organization", aliases=["TKG", "kg holdings", "HW"]),
            EntityName(name="TKG", entity_type="organization", aliases=["Kestrel Grp"]),
        ],
    )

    assert _rows(said) == [
        {"name": "Kestrel Group.", "id": "ent.the-kestrel-group", "created": False},
        {"name": "TKG", "id": "ent.the-kestrel-group", "created": False},
    ]
    assert _frontmatter(tmp_path, "the-kestrel-group")["aliases"] == ["ent.the-kestrel-group", "KG Holdings", "TKG", "Kestrel Grp"]
    assert _frontmatter(tmp_path, "harbor-works")["aliases"] == ["ent.harbor-works", "HW"], "a name another page holds stays that page's"
    text = (tmp_path / _ENTITIES / "the-kestrel-group.md").read_text(encoding="utf-8")
    assert "The Kestrel Group as another writer wrote it." in text, "the matched page keeps its own body"
    assert _lock_files(tmp_path) == [".the-kestrel-group.md.lock"], "the lock file is hidden"
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["directory"]


@pytest.mark.parametrize("subfolder", ["", "orgs/"])
async def test_a_new_name_whose_page_id_an_existing_page_already_carries_joins_that_page_wherever_it_sits(tmp_path: Path, subfolder: str) -> None:
    tools, reindex, knowledge_base = _tools(tmp_path)
    NoteWriter(knowledge_base).write(_entity_page("ent.kestrel-labs", "Kestrel Laboratories", []), f"{_ENTITIES}/{subfolder}kestrel-labs.md")

    said = await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="organization", aliases=["KSL"])])

    assert _rows(said) == [{"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": False}]
    assert _every_entity_file(tmp_path) == [f"{subfolder}kestrel-labs.md"], "no second file carries the id"
    frontmatter, _body = split_frontmatter((tmp_path / _ENTITIES / f"{subfolder}kestrel-labs.md").read_text(encoding="utf-8"))
    assert frontmatter["aliases"] == ["ent.kestrel-labs", "Kestrel Labs", "KSL"]
    assert len(reindex.knowledge_bases) == 1


async def test_a_name_that_matches_no_page_resolves_through_an_alias_that_does_and_joins_that_page(tmp_path: Path) -> None:
    tools, reindex, knowledge_base = _tools(tmp_path)
    NoteWriter(knowledge_base).write(_entity_page("ent.kestrel-labs", "Kestrel Labs", []))

    said = await tools.resolve_entities(
        "directory", [EntityName(name="Kestrel Laboratories", entity_type="organization", aliases=["Kestrel Labs"])]
    )

    assert _rows(said) == [{"name": "Kestrel Laboratories", "id": "ent.kestrel-labs", "created": False}]
    assert _entity_files(tmp_path) == ["kestrel-labs.md"]
    assert _frontmatter(tmp_path, "kestrel-labs")["aliases"] == ["ent.kestrel-labs", "Kestrel Laboratories"]
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["directory"]


async def test_an_alias_that_matches_no_page_leaves_an_unmatched_name_to_make_its_own_page(tmp_path: Path) -> None:
    tools, _, knowledge_base = _tools(tmp_path)
    NoteWriter(knowledge_base).write(_entity_page("ent.harbor-works", "Harbor Works", ["HW"]))

    said = await tools.resolve_entities("directory", [EntityName(name="Aardvark Inc", entity_type="organization", aliases=["AI Co"])])

    assert _rows(said) == [{"name": "Aardvark Inc", "id": "ent.aardvark-inc", "created": True}]
    assert _entity_files(tmp_path) == ["aardvark-inc.md", "harbor-works.md"]
    assert _frontmatter(tmp_path, "aardvark-inc")["aliases"] == ["ent.aardvark-inc", "AI Co"]
    assert _frontmatter(tmp_path, "harbor-works")["aliases"] == ["ent.harbor-works", "HW"]


async def test_the_first_alias_that_matches_picks_the_page_and_an_alias_another_page_holds_stays_that_page_s(tmp_path: Path) -> None:
    tools, _, knowledge_base = _tools(tmp_path)
    writer = NoteWriter(knowledge_base)
    writer.write(_entity_page("ent.harbor-works", "Harbor Works", ["HW"]))
    writer.write(_entity_page("ent.kestrel-labs", "Kestrel Labs", []))

    first = await tools.resolve_entities(
        "directory", [EntityName(name="Aardvark Inc", entity_type="organization", aliases=["AI Co", "HW", "Kestrel Labs"])]
    )
    second = await tools.resolve_entities("directory", [EntityName(name="AI Co", entity_type="organization")])

    assert _rows(first) == [{"name": "Aardvark Inc", "id": "ent.harbor-works", "created": False}]
    assert _entity_files(tmp_path) == ["harbor-works.md", "kestrel-labs.md"]
    assert _frontmatter(tmp_path, "harbor-works")["aliases"] == ["ent.harbor-works", "HW", "Aardvark Inc", "AI Co"]
    assert _frontmatter(tmp_path, "kestrel-labs")["aliases"] == ["ent.kestrel-labs"], "Kestrel Labs stays its own page's title"
    assert _rows(second) == [{"name": "AI Co", "id": "ent.harbor-works", "created": False}]


async def test_an_alias_that_matches_a_page_made_earlier_in_the_call_joins_that_page(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path)

    said = await tools.resolve_entities(
        "directory",
        [
            EntityName(name="Kestrel Labs", entity_type="organization", aliases=["KSL"]),
            EntityName(name="Kestrel Laboratories", entity_type="organization", aliases=["ksl"]),
        ],
    )

    assert _rows(said) == [
        {"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": True},
        {"name": "Kestrel Laboratories", "id": "ent.kestrel-labs", "created": True},
    ]
    assert _entity_files(tmp_path) == ["kestrel-labs.md"]
    assert _frontmatter(tmp_path, "kestrel-labs")["aliases"] == ["ent.kestrel-labs", "KSL", "Kestrel Laboratories"]


async def test_an_unmatched_name_creates_one_page_with_its_type_aliases_and_summary(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path)

    said = await tools.resolve_entities(
        "directory", [EntityName(name="Jordan Reyes", entity_type="person", aliases=["J. Reyes"], summary="A founder of Kestrel Labs.")]
    )

    assert _rows(said) == [{"name": "Jordan Reyes", "id": "ent.jordan-reyes", "created": True}]
    assert _entity_files(tmp_path) == ["jordan-reyes.md"]
    text = (tmp_path / _ENTITIES / "jordan-reyes.md").read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)
    assert frontmatter["type"] == "entity"
    assert frontmatter["title"] == "Jordan Reyes"
    assert frontmatter["entity_type"] == "person"
    assert frontmatter["aliases"] == ["ent.jordan-reyes", "J. Reyes"]
    assert frontmatter["summary"] == "A founder of Kestrel Labs."
    assert "## TL;DR\n\nA founder of Kestrel Labs." in body


async def test_a_new_page_in_the_research_template_takes_its_first_section_and_opening_status(tmp_path: Path) -> None:
    spec = KnowledgeBaseSchema.model_validate({**research_spec().model_dump(), "entities": {"page_type": "entity"}})
    tools, _, _ = _tools(tmp_path, spec=spec)

    said = await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="tool", summary="A maker of lab tools.")])

    assert _rows(said) == [{"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": True}]
    text = (tmp_path / "Wiki" / "Entities" / "kestrel-labs.md").read_text(encoding="utf-8")
    assert "## TL;DR\n\nA maker of lab tools." in text
    assert "entity_type: tool" in text


async def test_the_same_new_name_twice_in_one_call_creates_one_page(tmp_path: Path) -> None:
    tools, reindex, _ = _tools(tmp_path)

    said = await tools.resolve_entities(
        "directory",
        [
            EntityName(name="Kestrel Labs", entity_type="organization", aliases=["Kestrel"]),
            EntityName(name="kestrel labs!", entity_type="organization"),
            EntityName(name="Kestrel", entity_type="organization"),
        ],
    )

    assert _rows(said) == [
        {"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": True},
        {"name": "kestrel labs!", "id": "ent.kestrel-labs", "created": True},
        {"name": "Kestrel", "id": "ent.kestrel-labs", "created": True},
    ]
    assert _entity_files(tmp_path) == ["kestrel-labs.md"]
    assert len(reindex.knowledge_bases) == 1


async def test_two_new_names_that_make_the_same_page_id_fold_into_one_page_with_both_names(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path)

    said = await tools.resolve_entities(
        "directory", [EntityName(name="Zoë Works", entity_type="organization"), EntityName(name="Zo Works", entity_type="organization", aliases=["ZW"])]
    )

    assert _rows(said) == [{"name": "Zoë Works", "id": "ent.zo-works", "created": True}, {"name": "Zo Works", "id": "ent.zo-works", "created": True}]
    assert _entity_files(tmp_path) == ["zo-works.md"]
    assert _frontmatter(tmp_path, "zo-works")["aliases"] == ["ent.zo-works", "Zo Works", "ZW"]


class _RivalWriterAfterScan(EntityPages):
    """The real entity pages, except another writer puts `rival` at `<stem>.md` the moment the scan ends.

    A payload is rendered as the page it describes. A string is written as it is.
    """

    def __init__(self, knowledge_base: AuthoringKnowledgeBase, stem: str, rival: dict[str, object] | str) -> None:
        super().__init__(knowledge_base)
        self._knowledge_base = knowledge_base
        self._stem = stem
        self._rival = rival

    def known(self) -> KnownEntities:
        scanned = super().known()
        text = self._rival if isinstance(self._rival, str) else NoteWriter(self._knowledge_base).check(self._rival).rendered
        path = self._knowledge_base.path / _ENTITIES / f"{self._stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return scanned


async def test_a_name_another_writer_creates_after_the_scan_merges_both_alias_sets_into_that_page(tmp_path: Path) -> None:
    rival = _entity_page("ent.kestrel-labs", "Kestrel Labs", ["Kestrel Laboratories"])
    tools, reindex, _ = _tools(tmp_path, entity_pages_for=lambda kb: _RivalWriterAfterScan(kb, "kestrel-labs", rival))

    said = await tools.resolve_entities(
        "directory", [EntityName(name="Kestrel Labs", entity_type="organization", aliases=["KSL", "kestrel laboratories"])]
    )

    assert _rows(said) == [{"name": "Kestrel Labs", "id": "ent.kestrel-labs", "created": False}]
    assert _entity_files(tmp_path) == ["kestrel-labs.md"]
    assert _frontmatter(tmp_path, "kestrel-labs")["aliases"] == ["ent.kestrel-labs", "Kestrel Laboratories", "KSL"]
    text = (tmp_path / _ENTITIES / "kestrel-labs.md").read_text(encoding="utf-8")
    assert "Kestrel Labs as another writer wrote it." in text, "the other writer's page keeps its own body"
    assert len(reindex.knowledge_bases) == 1


@pytest.mark.parametrize(
    ("first", "second", "page"),
    [
        (EntityName(name="The Kestrel Group", entity_type="organization"), EntityName(name="Kestrel Group", entity_type="organization"), "the-kestrel-group.md"),
        (
            EntityName(name="Kestrel Labs", entity_type="organization", aliases=["KSL"]),
            EntityName(name="KSL", entity_type="organization", aliases=["Kestrel Labs"]),
            "kestrel-labs.md",
        ),
    ],
)
async def test_two_calls_at_once_naming_one_entity_with_different_page_ids_make_one_page(
    tmp_path: Path, first: EntityName, second: EntityName, page: str
) -> None:
    tools, reindex, _ = _tools(tmp_path)

    said = await asyncio.gather(tools.resolve_entities("directory", [first]), tools.resolve_entities("directory", [second]))

    assert _entity_files(tmp_path) == [page]
    assert [row["id"] for answer in said for row in _rows(answer)] == [f"ent.{page.removesuffix('.md')}"] * 2
    assert [row["created"] for answer in said for row in _rows(answer)] == [True, False]
    assert len(reindex.knowledge_bases) == 1


async def test_a_rival_page_with_its_own_id_at_the_planned_file_answers_that_id(tmp_path: Path) -> None:
    rival = _entity_page("ent.kestrel-laboratories", "Kestrel Laboratories", [])
    tools, _, _ = _tools(tmp_path, entity_pages_for=lambda kb: _RivalWriterAfterScan(kb, "kestrel-labs", rival))

    said = await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="organization")])

    assert _rows(said) == [{"name": "Kestrel Labs", "id": "ent.kestrel-laboratories", "created": False}]
    assert _frontmatter(tmp_path, "kestrel-labs")["aliases"] == ["ent.kestrel-laboratories", "Kestrel Labs"]


async def test_a_rival_file_that_is_no_entity_page_is_left_alone_and_the_refusal_names_the_pages_already_written(tmp_path: Path) -> None:
    tools, reindex, _ = _tools(tmp_path, entity_pages_for=lambda kb: _RivalWriterAfterScan(kb, "kestrel-labs", "no frontmatter here\n"))

    with pytest.raises(ModelRetry, match=r"kestrel-labs\.md already holds a file entity resolution cannot read as an entity page.*Pages already written: ent\.harbor-works"):
        await tools.resolve_entities(
            "directory", [EntityName(name="Harbor Works", entity_type="organization"), EntityName(name="Kestrel Labs", entity_type="organization")]
        )

    assert (tmp_path / _ENTITIES / "kestrel-labs.md").read_text(encoding="utf-8") == "no frontmatter here\n"
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["directory"], "the page already written is indexed"


async def test_every_entity_type_the_schema_does_not_declare_is_refused_at_once_naming_the_allowed_types(tmp_path: Path) -> None:
    tools, reindex, _ = _tools(tmp_path)

    with pytest.raises(ModelRetry, match=r"directory declares no entity_type 'place', 'thing'\. The entity types are organization, person"):
        await tools.resolve_entities(
            "directory",
            [
                EntityName(name="Kestrel Labs", entity_type="organization"),
                EntityName(name="Harbor Town", entity_type="place"),
                EntityName(name="Harbor Bell", entity_type="thing"),
                EntityName(name="Harbor Square", entity_type="place"),
            ],
        )

    assert _entity_files(tmp_path) == [], "a refusal writes nothing, not even the valid names before it"
    assert reindex.knowledge_bases == []


@pytest.mark.parametrize("name", ["Москва", "東京", "!!!", "Untitled"])
async def test_a_new_name_that_makes_no_page_id_is_refused_asking_for_a_latin_script_form(tmp_path: Path, name: str) -> None:
    tools, reindex, _ = _tools(tmp_path)

    with pytest.raises(ModelRetry, match="Latin-script form"):
        await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="organization"), EntityName(name=name, entity_type="organization")])

    assert _entity_files(tmp_path) == []
    assert reindex.knowledge_bases == []


async def test_every_name_that_makes_no_page_id_is_named_in_one_refusal(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path)

    with pytest.raises(ModelRetry, match=r"these names make no page id of their own: 'Москва', '東京'\. Pass a Latin-script form"):
        await tools.resolve_entities(
            "directory",
            [
                EntityName(name="Москва", entity_type="organization"),
                EntityName(name="Kestrel Labs", entity_type="organization"),
                EntityName(name="東京", entity_type="organization"),
            ],
        )

    assert _entity_files(tmp_path) == []


@pytest.mark.parametrize("name", ["", "   "])
async def test_a_blank_name_is_refused_with_its_position(tmp_path: Path, name: str) -> None:
    tools, reindex, _ = _tools(tmp_path)

    with pytest.raises(ModelRetry, match=r"blank name at position 2 of entities, counted from 1\. Give every entity the name the source writes"):
        await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="organization"), EntityName(name=name, entity_type="organization")])

    assert _entity_files(tmp_path) == []
    assert reindex.knowledge_bases == []


async def test_a_name_in_another_script_that_matches_an_alias_resolves_without_a_page_id_of_its_own(tmp_path: Path) -> None:
    tools, _, knowledge_base = _tools(tmp_path)
    NoteWriter(knowledge_base).write(_entity_page("ent.harbor-town", "Harbor Town", ["Гавань"], entity_type="organization"))

    said = await tools.resolve_entities("directory", [EntityName(name="гавань", entity_type="organization")])

    assert _rows(said) == [{"name": "гавань", "id": "ent.harbor-town", "created": False}]


async def test_a_knowledge_base_whose_schema_names_no_entity_page_type_is_refused(tmp_path: Path) -> None:
    tools, reindex, _ = _tools(tmp_path, spec=_SPEC.model_copy(update={"entities": None}))

    with pytest.raises(ModelRetry, match=r"directory keeps no entity pages: its schema has no entities block"):
        await tools.resolve_entities("directory", [EntityName(name="Kestrel Labs", entity_type="organization")])

    assert reindex.knowledge_bases == []


async def test_the_call_reindexes_once_however_many_pages_it_wrote(tmp_path: Path) -> None:
    tools, reindex, _ = _tools(tmp_path)

    await tools.resolve_entities(
        "directory",
        [
            EntityName(name="Kestrel Labs", entity_type="organization"),
            EntityName(name="Jordan Reyes", entity_type="person"),
            EntityName(name="Harbor Works", entity_type="organization"),
        ],
    )

    assert _entity_files(tmp_path) == ["harbor-works.md", "jordan-reyes.md", "kestrel-labs.md"]
    assert [knowledge_base.name for knowledge_base in reindex.knowledge_bases] == ["directory"]


def test_resolve_entities_is_one_of_the_tools_every_acting_step_gets(tmp_path: Path) -> None:
    tools, _, _ = _tools(tmp_path)
    assert "resolve_entities" in [name for _call, name, _description in tools.tools()]
