"""Unit tests for the pure link/loc resolver (`src/engram/indexing/domain/resolver.py`).

These pin the binding *Resolve* contract (see `docs/reference/compile.md`):

* a link index maps every note's **filename stem + aliases + id** to its page id,
  replicating Obsidian's case-insensitive unit link resolution;
* unit links in frontmatter relations and claim fields resolve to ids; an
  unresolved target is a **validation warning** (the row still loads — flagged-but-loaded);
* a dangling `builds_on` (a paper-ranged relation) is the **ingest queue**, not a
  warning: a wanted-but-not-yet-ingested paper;
* `loc::` `§`-paths index to an `ltree` (`§4.1, Table 2` → `s4.s1`);
  `Table`/`Fig`/`p.` parts stay locator qualifiers, never path segments.

No DB, no IO: the resolver is a pure function over parsed notes + the vocabulary.
Fixtures are built from the memory domain factories plus small hand-written notes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import resolve, section_loc
from engram.indexing.domain.resolver import build_link_index, loc_to_ltree
from engram.indexing.service.row_builder import link_ref_rows
from engram.kb.contracts import ClaimBlock, KnowledgeBaseNote, KnowledgeBaseSchema, PageTypeSpec, Section, UnitLink, parse_frontmatter
from engram.kb.contracts import parse as parse_note
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from engram.indexing.domain.resolver import ResolveResult

_RESEARCH_SPEC = research_spec()


def _paper_note(
    *,
    id_: str = "paper.lightrag-2025",
    title: str = "LightRAG",
    aliases: tuple[str, ...] = (),
    source_path: str = "Memory/Papers/lightrag-2025.md",
    builds_on: tuple[str, ...] = (),
    claims: tuple[ClaimBlock, ...] = (),
    sections: tuple[Section, ...] = (),
) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": id_,
        "type": "paper",
        "title": title,
        "status": "draft",
        "aliases": list(aliases),
        "paper_type": "method",
        "year": 2025,
        "zotero_key": "K",
        "source_hash": "h",
        "builds_on": [f"[[{t}]]" for t in builds_on],
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=sections, claims=claims, source_path=source_path)


def _entity_note(*, id_: str, title: str, aliases: tuple[str, ...] = (), source_path: str | None = None) -> KnowledgeBaseNote:
    data: dict[str, object] = {
        "id": id_,
        "type": "entity",
        "title": title,
        "status": "draft",
        "aliases": list(aliases),
        "entity_type": "technique",
    }
    frontmatter = parse_frontmatter(data, _RESEARCH_SPEC)
    path = source_path or f"Memory/Entities/{id_.removeprefix('ent.')}.md"
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=path)


def _moc_note(*, id_: str = "moc.test", title: str = "Test Map", body: str) -> KnowledgeBaseNote:
    text = f"---\nid: {id_}\ntype: moc\ntitle: {title}\nstatus: draft\n---\n\n{body}\n"
    return parse_note(text, source_path=f"Wiki/MOCs/{id_.removeprefix('moc.')}.md", spec=_RESEARCH_SPEC)


class TestLinkIndex:
    """The index maps filename stem + aliases + id to the page id, case-insensitively."""

    def test_filename_stem_resolves_to_id(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG", source_path="Memory/Entities/lightrag.md")

        index = build_link_index((note,))

        assert index.resolve(UnitLink(target="lightrag")) == "ent.lightrag"

    def test_alias_resolves_to_id(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG", aliases=("LightRAG (method)", "light-rag"))

        index = build_link_index((note,))

        assert index.resolve(UnitLink(target="light-rag")) == "ent.lightrag"

    def test_title_resolves_to_id(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG", source_path="Memory/Entities/lr.md")

        index = build_link_index((note,))

        assert index.resolve(UnitLink(target="LightRAG")) == "ent.lightrag"

    def test_resolution_is_case_insensitive(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG", source_path="Memory/Entities/LightRAG.md")

        index = build_link_index((note,))

        assert index.resolve(UnitLink(target="lightrag")) == "ent.lightrag"
        assert index.resolve(UnitLink(target="LIGHTRAG")) == "ent.lightrag"

    def test_unknown_target_resolves_to_none(self):
        index = build_link_index(())

        assert index.resolve(UnitLink(target="nonexistent")) is None

    def test_id_resolves_to_itself(self):
        note = _entity_note(id_="ent.lightrag", title="LightRAG")

        index = build_link_index((note,))

        assert index.resolve(UnitLink(target="ent.lightrag")) == "ent.lightrag"


class TestFrontmatterAndClaimResolution:
    """Frontmatter relations and claim fields resolve to ids; misses become warnings."""

    def test_resolved_claim_fields_carry_ids(self):
        target = _entity_note(id_="ent.graphrag", title="GraphRAG")
        dataset = _entity_note(id_="ent.ultradomain", title="UltraDomain")
        metric = _entity_note(id_="ent.win-rate", title="Win Rate", aliases=("llm-judged-win-rate",))
        claim = ClaimBlock(
            anchor="^c1",
            text="LightRAG beats GraphRAG.",
            loc="§4.2",
            kind="benchmark-result",
            edges=(("outperforms", UnitLink(target="graphrag")),),
            links={"on_dataset": (UnitLink(target="ultradomain"),), "metric": (UnitLink(target="llm-judged-win-rate"),)},
        )
        note = _paper_note(claims=(claim,))

        result = resolve((note, target, dataset, metric))
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "paper.lightrag-2025")
        rclaim = resolved.claims[0]

        assert rclaim.link_ids == {"on_dataset": ("ent.ultradomain",), "metric": ("ent.win-rate",)}
        assert rclaim.edges[0].dst_id == "ent.graphrag"

    def test_multi_link_edge_line_compiles_into_two_resolvable_edges(self):
        # A single `supports:: [[a]], [[b]]` line parses into two edges (parser.py), and each
        # resolves to its own target id — the multi-target grammar round-tripping end to end.
        f1 = _entity_note(id_="ent.f1-score", title="F1 score")
        exact_match = _entity_note(id_="ent.exact-match", title="Exact match")
        text = (
            "---\nid: paper.sample-2025\ntype: paper\ntitle: Sample\nstatus: draft\npaper_type: method\n"
            "year: 2025\nzotero_key: K\nsource_hash: h\n---\n\n"
            "## Findings\n\n- A. ^c1\n    - loc:: §1\n    - supports:: [[f1-score]], [[exact-match]]\n"
        )
        note = parse_note(text, source_path="Memory/Papers/sample-2025.md", spec=research_spec())

        result = resolve((note, f1, exact_match))
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "paper.sample-2025")
        rclaim = resolved.claims[0]

        assert [e.dst_id for e in rclaim.edges] == ["ent.f1-score", "ent.exact-match"]

    def test_unresolved_claim_link_is_a_warning(self):
        claim = ClaimBlock(anchor="^c1", text="A.", loc="§1", kind="observation", subject=UnitLink(target="ghost-entity"))
        note = _paper_note(claims=(claim,))

        result = resolve((note,))

        assert any(w.rule == "dangling-link" and "ghost-entity" in w.message and w.anchor == "^c1" for w in result.warnings)

    def test_resolved_frontmatter_edge_carries_id(self):
        target = _entity_note(id_="ent.lightrag", title="LightRAG")
        note = KnowledgeBaseNote(
            frontmatter=parse_frontmatter(
                {
                    "id": "paper.x",
                    "type": "paper",
                    "title": "X",
                    "status": "draft",
                    "paper_type": "method",
                    "year": 2025,
                    "zotero_key": "K",
                    "source_hash": "h",
                    "proposes": ["[[lightrag]]"],
                },
                _RESEARCH_SPEC,
            ),
            sections=(),
            claims=(),
            source_path="Memory/Papers/x.md",
        )

        result = resolve((note, target))
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "paper.x")

        assert any(e.rel == "proposes" and e.dst_id == "ent.lightrag" for e in resolved.edges)


_TOPIC_SPEC = KnowledgeBaseSchema(slug="topics", page_types=(PageTypeSpec(page_type="topic", id_prefix="topic."),))


def _topic_note(stem: str, *, superseded_by: str | None = None) -> KnowledgeBaseNote:
    link = f'superseded_by: "[[{superseded_by}]]"\n' if superseded_by is not None else ""
    text = f"---\nid: topic.{stem}\ntype: topic\ntitle: {stem.title()}\nstatus: draft\n{link}---\n"
    return parse_note(text, source_path=f"Units/topic/{stem}.md", spec=_TOPIC_SPEC)


class TestSupersededBy:
    """The frontmatter `superseded_by` link resolves to the id of the page it names, or to nothing on a miss."""

    def test_a_link_by_file_name_resolves_to_the_page_id(self):
        result = resolve((_topic_note("alpha", superseded_by="beta"), _topic_note("beta")))
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "topic.alpha")

        assert resolved.superseded_by_id == "topic.beta"

    def test_a_link_naming_no_page_resolves_to_none_and_is_a_warning(self):
        result = resolve((_topic_note("alpha", superseded_by="gamma"),))

        assert result.notes[0].superseded_by_id is None
        assert [(w.rule, w.page_id, w.message) for w in result.warnings] == [
            ("dangling-link", "topic.alpha", "unresolved superseded_by link 'gamma'")
        ]


class TestDeclaredClaimFields:
    """Every declared link field resolves by its own name, and every declared text field rides on each edge."""

    def test_every_declared_link_field_resolves_by_name_in_written_order(self):
        teams = (_entity_note(id_="ent.team-a", title="Team A"), _entity_note(id_="ent.team-b", title="Team B"))
        policy = _entity_note(id_="ent.policy", title="Policy")
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            links={
                "applies_to": (UnitLink(target="ent.team-b"), UnitLink(target="ent.ghost"), UnitLink(target="ent.team-a")),
                "governed_by": (UnitLink(target="ent.policy"),),
            },
        )
        note = _paper_note(claims=(claim,))

        result = resolve((note, *teams, policy))
        rclaim = next(n for n in result.notes if n.note.frontmatter.id == "paper.lightrag-2025").claims[0]

        assert rclaim.link_ids == {"applies_to": ("ent.team-b", "ent.team-a"), "governed_by": ("ent.policy",)}
        assert [(w.rule, w.anchor, w.message) for w in result.warnings] == [
            ("dangling-link", "^c1", "unresolved applies_to link 'ent.ghost'")
        ]

    def test_every_declared_text_field_is_copied_onto_each_edge(self):
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds both teams.",
            loc="§2",
            edges=(("governs", UnitLink(target="ent.team-a")), ("cites", UnitLink(target="ent.team-b"))),
            fields={"jurisdiction": "EU", "scope": "tenants", "remark": ""},
        )
        note = _paper_note(claims=(claim,))

        result = resolve((note, _entity_note(id_="ent.team-a", title="Team A"), _entity_note(id_="ent.team-b", title="Team B")))
        rclaim = next(n for n in result.notes if n.note.frontmatter.id == "paper.lightrag-2025").claims[0]

        assert [edge.qualifiers for edge in rclaim.edges] == [{"jurisdiction": "EU", "scope": "tenants"}] * 2


class TestClaimFieldLinks:
    """Every target of a claim's subject, link fields and `disputed_by` is kept as a field link, resolved or dangling."""

    @staticmethod
    def _resolved_claim_note() -> ResolveResult:
        claim = ClaimBlock(
            anchor="^c1",
            text="The policy binds the team.",
            loc="§2",
            subject=UnitLink(target="ent.ghost"),
            edges=(("governs", UnitLink(target="ent.team-a")),),
            links={"applies_to": (UnitLink(target="Team A"), UnitLink(target="ent.missing"))},
            disputed_by=(UnitLink(target="paper.other", block="c2"),),
        )
        return resolve((_paper_note(claims=(claim,)), _entity_note(id_="ent.team-a", title="Team A")))

    def test_each_field_target_is_one_field_link_in_written_order(self):
        result = self._resolved_claim_note()
        rclaim = next(n for n in result.notes if n.note.frontmatter.id == "paper.lightrag-2025").claims[0]

        assert [(link.rel, link.dst_target, link.dst_id) for link in rclaim.field_links] == [
            ("subject", "ent.ghost", None),
            ("applies_to", "Team A", "ent.team-a"),
            ("applies_to", "ent.missing", None),
            ("disputed_by", "paper.other", None),
        ]
        assert {(link.src_id, link.src_type, link.provenance_claim) for link in rclaim.field_links} == {
            ("paper.lightrag-2025#c1", "claim", "paper.lightrag-2025#c1")
        }

    def test_the_link_references_record_every_field_link_beside_the_claim_edges(self):
        rows = link_ref_rows(self._resolved_claim_note(), "research")
        claim_rows = [(row["rel"], row["raw_target"], row["dst_id"]) for row in rows if row["src_page_id"] == "paper.lightrag-2025"]

        assert claim_rows == [
            ("governs", "ent.team-a", "ent.team-a"),
            ("subject", "ent.ghost", None),
            ("applies_to", "team a", "ent.team-a"),
            ("applies_to", "ent.missing", None),
            ("disputed_by", "paper.other", None),
        ]


class TestIngestQueue:
    """A dangling link of a relation the schema flags `queue_when_missing` is the ingest queue, not a warning."""

    def test_dangling_builds_on_goes_to_ingest_queue(self):
        note = _paper_note(builds_on=("graphrag-2024",))

        result = resolve((note,), relations=_RESEARCH_SPEC.relations)

        assert "graphrag-2024" in result.ingest_queue

    def test_dangling_builds_on_is_not_a_warning(self):
        note = _paper_note(builds_on=("graphrag-2024",))

        result = resolve((note,), relations=_RESEARCH_SPEC.relations)

        assert not any("graphrag-2024" in w.message for w in result.warnings)

    def test_resolved_builds_on_is_not_in_the_queue(self):
        target = _paper_note(id_="paper.graphrag-2024", title="GraphRAG", source_path="Memory/Papers/graphrag-2024.md")
        note = _paper_note(builds_on=("graphrag-2024",))

        result = resolve((note, target), relations=_RESEARCH_SPEC.relations)

        assert result.ingest_queue == ()

    def test_the_research_template_flags_builds_on_and_synthesizes(self):
        flagged = {relation.slug for relation in _RESEARCH_SPEC.relations if relation.queue_when_missing}

        assert flagged == {"builds_on", "synthesizes"}

    def test_builds_on_without_the_flag_is_a_warning_like_any_other_link(self):
        unflagged = tuple(relation.model_copy(update={"queue_when_missing": False}) for relation in _RESEARCH_SPEC.relations)
        note = _paper_note(builds_on=("graphrag-2024",))

        result = resolve((note,), relations=unflagged)

        assert result.ingest_queue == ()
        assert any(w.rule == "dangling-link" and "graphrag-2024" in w.message for w in result.warnings)


class TestLocToLtree:
    """`loc::` `§`-paths index to ltree labels; Table/Fig/p. stay qualifiers."""

    def test_section_path_becomes_ltree(self):
        assert loc_to_ltree("§4.1") == "s4.s1"

    def test_section_with_table_qualifier_drops_the_table(self):
        assert loc_to_ltree("§4.1, Table 2") == "s4.s1"

    def test_three_level_section_path(self):
        assert loc_to_ltree("§4.1.3") == "s4.s1.s3"

    def test_single_level_section_path(self):
        assert loc_to_ltree("§5") == "s5"

    def test_table_only_loc_has_no_path(self):
        assert loc_to_ltree("Table 2") is None

    def test_figure_only_loc_has_no_path(self):
        assert loc_to_ltree("Fig. 2") is None

    def test_page_only_loc_has_no_path(self):
        assert loc_to_ltree("p.7") is None

    def test_resolved_claim_keeps_raw_loc_and_derives_path(self):
        claim = ClaimBlock(anchor="^c1", text="A.", loc="§4.1, Table 2", kind="observation")
        note = _paper_note(claims=(claim,))

        result = resolve((note,))
        rclaim = next(n for n in result.notes if n.note.frontmatter.id == "paper.lightrag-2025").claims[0]

        assert rclaim.loc == "§4.1, Table 2"
        assert rclaim.loc_path == "s4.s1"


def test_section_loc_is_the_inverse_of_loc_to_ltree() -> None:
    assert section_loc("s4.s1") == "§4.1"
    assert section_loc("s5") == "§5"
    assert section_loc("s4.s1.s3") == "§4.1.3"
    assert loc_to_ltree(section_loc("s4.s1") or "") == "s4.s1"


def test_section_loc_of_an_unnumbered_chunk_is_none() -> None:
    assert section_loc(None) is None
    assert section_loc("") is None


class TestSectionDerivedEdges:
    """`RelationDecl.domain_section` (the research spec's `maps` relation) derives edges from a domain page's body.

    Never from frontmatter, matching the moc page type's `map` canonical section, wikilinks
    nested under it included.
    """

    def test_a_map_sections_links_become_maps_edges_in_document_order(self):
        alpha = _entity_note(id_="ent.alpha", title="Alpha")
        beta = _entity_note(id_="ent.beta", title="Beta")
        gamma = _entity_note(id_="ent.gamma", title="Gamma")
        moc = _moc_note(body="## TL;DR\n\nOverview.\n\n## Map\n\n### Group\n\n- [[alpha]] first.\n- [[beta]] second.\n- [[gamma]] third.\n")

        result = resolve((moc, alpha, beta, gamma), relations=_RESEARCH_SPEC.relations)
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "moc.test")
        map_edges = [e for e in resolved.edges if e.rel == "maps"]

        assert [e.dst_id for e in map_edges] == ["ent.alpha", "ent.beta", "ent.gamma"]
        assert all(e.src_id == "moc.test" and e.src_type == "page" for e in map_edges)

    def test_a_repeated_link_in_the_map_section_becomes_one_edge(self):
        alpha = _entity_note(id_="ent.alpha", title="Alpha")
        moc = _moc_note(body="## Map\n\n### Group\n\n- [[alpha]] first.\n- [[alpha]] again.\n")

        result = resolve((moc, alpha), relations=_RESEARCH_SPEC.relations)
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "moc.test")

        assert [e.dst_id for e in resolved.edges if e.rel == "maps"] == ["ent.alpha"]

    def test_a_moc_page_with_no_links_in_its_map_section_emits_no_maps_edges(self):
        moc = _moc_note(body="## Map\n\nNo links here.\n")

        result = resolve((moc,), relations=_RESEARCH_SPEC.relations)
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "moc.test")

        assert resolved.edges == ()

    def test_a_page_of_another_type_with_a_map_heading_emits_no_maps_edges(self):
        alpha = _entity_note(id_="ent.alpha", title="Alpha")
        text = (
            "---\nid: paper.decoy\ntype: paper\ntitle: Decoy\nstatus: draft\npaper_type: method\n"
            "year: 2025\nzotero_key: K\nsource_hash: h\n---\n\n## Map\n\n- [[alpha]] not a moc.\n"
        )
        paper = parse_note(text, source_path="Memory/Papers/decoy.md", spec=_RESEARCH_SPEC)

        result = resolve((paper, alpha), relations=_RESEARCH_SPEC.relations)
        resolved = next(n for n in result.notes if n.note.frontmatter.id == "paper.decoy")

        assert not any(e.rel == "maps" for e in resolved.edges)

    def test_a_dangling_link_in_the_map_section_is_a_warning_not_the_ingest_queue(self):
        moc = _moc_note(body="## Map\n\n### Group\n\n- [[ghost-page]] missing.\n")

        result = resolve((moc,), relations=_RESEARCH_SPEC.relations)

        assert any(w.rule == "dangling-link" and "ghost-page" in w.message for w in result.warnings)
        assert "ghost-page" not in result.ingest_queue
