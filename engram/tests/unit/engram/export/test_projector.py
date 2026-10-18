r"""Unit tests for the pure OKF projector (`src/engram/export/domain/projector.py`).

The projector is the heart of the slice and is pure (no IO), so it is tested hardest.
The arrange path parses real memory-note markdown through the shared grammar
(:func:`engram.kb.domain.parser.parse`) and resolves it with the indexing slice's
:func:`engram.indexing.domain.resolver.resolve` — so the projector is exercised against
genuine :class:`~engram.indexing.domain.models.ResolvedNote`\s (real frontmatter, real typed
edges), not hand-built stand-ins.

Every fixture here belongs to a **made-up knowledge base** declared in this module
(`_FIELDGUIDE_SPEC`: `record` / `specimen` / `atlas` / `meta` page_types, `habitats` and
`seasons` facets, a source layer keyed on `catalogue_key`). Nothing the projector does may
depend on a particular knowledge base's vocabulary, so the specs prove it against one no bundle
ships. These specs pin:

* the concept **path** (subdir from the page_type's own declared folder + slug from its own id
  prefix) and that `meta` pages are skipped;
* the OKF **frontmatter**: `type` is the page type verbatim, plus `title` / `description` /
  `tags` and the omit-when-absent rule;
* the **v0.2 trust + lifecycle families**: `status` (the QA ladder folded into
  `draft | stable | deprecated`, supersession wins), `generated` (`{by, at}`, the
  tool actor + the `updated` date), `verified` (one machine event for QA-`verified`
  pages), and `sources` (the entry a page naming a source carries, its `resource` the deep
  link that source's own mirror supplies);
* the **extension keys** `okf_x_relations` (typed edges → resolved concept paths)
  and `okf_x_engram` (id / status / collection / facets / source key + hash / the long tail
  under `fields`);
* the **body** unit link rewriting (resolved → relative link, dangling → plain text,
  fence-aware: a `[[x]]` inside a code fence is left untouched) and the
  `## Relations` section;
* the bundle **index.md** (frontmatter is `okf_version: "0.2"` alone, the v0.2
  reserved-file structure, + a grouped directory listing) and OKF **conformance**
  (every concept carries a non-empty `type`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.export.domain.projector import _subdir_for_page_type, project_bundle, project_with_counts
from engram.indexing.contracts import resolve
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec, parse

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from engram.export.contracts import OkfBundle, OkfConcept
    from engram.indexing.contracts import ResolvedNote

# -- the made-up knowledge base this module projects ------------------------------------

_FIELDGUIDE_SPEC = KnowledgeBaseSchema.model_validate(
    {
        "version": "0.1",
        "slug": "fieldguide",
        "page_types": [
            {
                "page_type": "record",
                "id_prefix": "rec.",
                "folder": "Vault/Records",
                "frontmatter_fields": [
                    {"name": "describes", "type": "link-list"},
                    {"name": "follows", "type": "link-list"},
                ],
                "edge_fields": ["describes", "follows"],
            },
            {
                "page_type": "specimen",
                "id_prefix": "spc.",
                "folder": "Vault/Specimens",
                "frontmatter_fields": [{"name": "resembles", "type": "link-list"}],
                "edge_fields": ["resembles"],
            },
            {"page_type": "atlas", "id_prefix": "atl.", "folder": "Vault/Atlases"},
            {"page_type": "meta", "id_prefix": "meta.", "folder": "Vault/_meta"},
        ],
        "facets": [{"key": "habitats"}, {"key": "seasons"}],
        "sources": {
            "enabled": True,
            "connectors": ["catalogue"],
            "key_field": "catalogue_key",
            "digest_link_field": "catalogued_in",
            "hash_field": "catalogue_hash",
            "digest_types": {"article": "record"},
        },
    }
)

# The deep link the knowledge base's own source mirror carries for `CAT-1`; `CAT-9` (the second
# record's key) is deliberately absent, so its entry must come back without a `resource`.
_SOURCE_LINKS = {"CAT-1": "catalogue://items/CAT-1"}

_RECORD = """\
---
id: rec.coastal-survey
type: record
title: Coastal survey
aliases: [The coastal survey]
summary: What the tide pools held in spring.
status: synthesized
created: 2026-06-01
updated: 2026-06-10
catalogue_key: CAT-1
catalogue_hash: bd72eebf
season_notes: taken at low tide
attachments:
  plates: https://example.invalid/plates
tags: [tidal, survey]
habitats: [shoreline]
seasons: [spring, summer]
describes: ["[[limpet]]"]
follows: ["[[rec.dune-survey]]"]
---

## Overview

The survey counted [[limpet]] colonies. It never reached [[a missing place]].

## Details

Counts were taken twice a day.
"""

_SPECIMEN = """\
---
id: spc.limpet
type: specimen
title: Limpet
aliases: [Common limpet]
summary: A grazing marine snail.
status: draft
updated: 2026-06-09
habitats: [shoreline]
resembles: ["[[periwinkle]]"]
---

## Overview

A grazing marine snail. See the [[coastal-survey|coastal survey]].

## Anatomy

Holds fast to rock.

```python
# a fenced block: [[periwinkle]] here must NOT be rewritten
count("[[limpet]]")
```
"""

_PERIWINKLE = """\
---
id: spc.periwinkle
type: specimen
title: Periwinkle
status: draft
---

## Overview

Another grazing marine snail.
"""

_META = """\
---
id: meta.about
type: meta
title: About
status: verified
---

## Body

Contract text — not knowledge, must be skipped.
"""

_ATLAS = """\
---
id: atl.shoreline
type: atlas
title: Shoreline atlas
status: draft
---

## Map

- [[limpet]]
"""


def _resolve(markdown_by_path: Iterable[tuple[str, str]], spec: KnowledgeBaseSchema = _FIELDGUIDE_SPEC) -> tuple[ResolvedNote, ...]:
    """Parse each `(source_path, text)` against `spec` and resolve them together (one link index)."""
    notes = [parse(text, source_path=path, spec=spec) for path, text in markdown_by_path]
    return resolve(note for note in notes).notes


@pytest.fixture
def resolved() -> tuple[ResolvedNote, ...]:
    """The five-note knowledge base (a record + two specimens + a meta page + an atlas) resolved together."""
    return _resolve(
        [
            ("Vault/Records/coastal-survey.md", _RECORD),
            ("Vault/Specimens/limpet.md", _SPECIMEN),
            ("Vault/Specimens/periwinkle.md", _PERIWINKLE),
            ("Vault/_meta/about.md", _META),
            ("Vault/Atlases/shoreline.md", _ATLAS),
        ]
    )


@pytest.fixture
def bundle(resolved: tuple[ResolvedNote, ...]) -> OkfBundle:
    """The whole knowledge base projected with the mirror's source links threaded in."""
    return project_bundle(resolved, collection="fieldguide", schema=_FIELDGUIDE_SPEC, source_links=_SOURCE_LINKS)


def _concept(bundle: OkfBundle, path: str) -> OkfConcept:
    """The concept at `path` (a clear failure if the projection did not emit it)."""
    return next(concept for concept in bundle.concepts if concept.path == path)


def _engram(concept: OkfConcept) -> Mapping[str, object]:
    """The concept's `okf_x_engram` overlay narrowed to a mapping (it always is one)."""
    overlay = concept.frontmatter["okf_x_engram"]
    assert isinstance(overlay, dict)
    return overlay


def _fields(concept: OkfConcept) -> Mapping[str, object]:
    """The overlay's long-tail `fields` bag narrowed to a mapping (empty when absent)."""
    bag = _engram(concept).get("fields", {})
    assert isinstance(bag, dict)
    return bag


def _relations(concept: OkfConcept) -> Mapping[str, object]:
    """The concept's `okf_x_relations` overlay narrowed to a mapping (empty when absent)."""
    overlay = concept.frontmatter.get("okf_x_relations", {})
    assert isinstance(overlay, dict)
    return overlay


# -- path + skip-meta -----------------------------------------------------------


class TestConceptPathAndSkips:
    def test_groups_by_kind_into_subdirs_with_slug_from_id(self, bundle: OkfBundle):
        paths = {concept.path for concept in bundle.concepts}
        assert paths == {"records/coastal-survey.md", "specimens/limpet.md", "specimens/periwinkle.md", "atlases/shoreline.md"}

    def test_meta_pages_are_skipped(self, bundle: OkfBundle):
        assert all(not concept.path.startswith("_meta") for concept in bundle.concepts)
        assert all(concept.frontmatter.get("type") != "meta" for concept in bundle.concepts)

    def test_counts_the_skipped_meta_pages(self, resolved: tuple[ResolvedNote, ...]):
        result = project_with_counts(resolved, collection="fieldguide", schema=_FIELDGUIDE_SPEC)

        assert len(result.bundle.concepts) == 4
        assert result.skipped_meta == 1


# -- frontmatter ---------------------------------------------------------------


class TestFrontmatter:
    def test_type_is_the_page_kind_verbatim(self, bundle: OkfBundle):
        # OKF's `type` is an uncurated free string, so the knowledge base's own page_type is the value —
        # no page_type of any knowledge base is renamed on the way out.
        assert _concept(bundle, "records/coastal-survey.md").frontmatter["type"] == "record"
        assert _concept(bundle, "specimens/limpet.md").frontmatter["type"] == "specimen"
        assert _concept(bundle, "atlases/shoreline.md").frontmatter["type"] == "atlas"

    def test_title_description_and_tags(self, bundle: OkfBundle):
        front = _concept(bundle, "records/coastal-survey.md").frontmatter
        assert front["title"] == "Coastal survey"
        assert front["description"] == "What the tide pools held in spring."
        assert front["tags"] == ["tidal", "survey"]  # the free-form tags, distinct from the declared facets
        assert "timestamp" not in front  # v0.2 replaced `timestamp` with `generated.at`

    def test_description_omitted_when_summary_absent(self, bundle: OkfBundle):
        # The periwinkle has no summary → no description key at all (OKF: omit optional fields).
        assert "description" not in _concept(bundle, "specimens/periwinkle.md").frontmatter

    def test_no_top_level_resource_is_invented(self, bundle: OkfBundle):
        # A page-level `resource` would have to be read out of some knowledge base's own field name;
        # the long tail carries such a link instead, under `fields`.
        assert all("resource" not in concept.frontmatter for concept in bundle.concepts)


# -- v0.2 trust + lifecycle families ---------------------------------------------

_VERIFIED_SPECIMEN = """\
---
id: spc.mussel
type: specimen
title: Mussel
status: verified
updated: 2026-06-12
---

## Overview

A bivalve of the rocky shore.
"""

_SUPERSEDED_SPECIMEN = """\
---
id: spc.old-mussel
type: specimen
title: Mussel (older entry)
status: synthesized
updated: 2026-06-11
superseded_by: "[[mussel]]"
---

## Overview

The entry the newer one replaced.
"""

_SECOND_RECORD = """\
---
id: rec.dune-survey
type: record
title: Dune survey
status: draft
catalogue_key: CAT-9
---

## Overview

A second survey, catalogued but with no deep link of its own.
"""


class TestV02TrustAndLifecycle:
    """The v0.2 upgrade: `status` / `generated` / `verified` / `sources` (timestamp replaced)."""

    @pytest.fixture
    def v02_bundle(self) -> OkfBundle:
        """The main knowledge base plus a QA-verified page, a superseded one, and a second record."""
        extra = _resolve(
            [
                ("Vault/Records/coastal-survey.md", _RECORD),
                ("Vault/Records/dune-survey.md", _SECOND_RECORD),
                ("Vault/Specimens/limpet.md", _SPECIMEN),
                ("Vault/Specimens/periwinkle.md", _PERIWINKLE),
                ("Vault/Specimens/mussel.md", _VERIFIED_SPECIMEN),
                ("Vault/Specimens/old-mussel.md", _SUPERSEDED_SPECIMEN),
            ]
        )
        return project_bundle(extra, collection="fieldguide", schema=_FIELDGUIDE_SPEC, source_links=_SOURCE_LINKS, generated_by="engram/9.9")

    def test_status_folds_the_qa_ladder(self, v02_bundle: OkfBundle):
        # draft (not yet reviewed) ← stub | draft; stable (ready) ← every other rung.
        assert _concept(v02_bundle, "specimens/limpet.md").frontmatter["status"] == "draft"
        assert _concept(v02_bundle, "records/coastal-survey.md").frontmatter["status"] == "stable"
        assert _concept(v02_bundle, "specimens/mussel.md").frontmatter["status"] == "stable"

    def test_supersession_wins_as_deprecated(self, v02_bundle: OkfBundle):
        # A superseded page is deprecated regardless of its QA rung (kept for links + history).
        assert _concept(v02_bundle, "specimens/old-mussel.md").frontmatter["status"] == "deprecated"

    def test_generated_carries_the_actor_and_the_updated_date(self, v02_bundle: OkfBundle):
        assert _concept(v02_bundle, "records/coastal-survey.md").frontmatter["generated"] == {"by": "engram/9.9", "at": "2026-06-10"}

    def test_generated_omits_at_when_the_page_has_no_updated_date(self, v02_bundle: OkfBundle):
        # The periwinkle has no `updated`; `by` is required within `generated`, `at` is not.
        assert _concept(v02_bundle, "specimens/periwinkle.md").frontmatter["generated"] == {"by": "engram/9.9"}

    def test_verified_event_only_for_qa_verified_pages(self, v02_bundle: OkfBundle):
        # One machine-confirmation event (the tool actor → machine-confirmed trust tier).
        assert _concept(v02_bundle, "specimens/mussel.md").frontmatter["verified"] == [{"by": "engram/9.9", "at": "2026-06-12"}]
        # Lower QA rungs carry no verified events at all (absent key, not an empty list).
        assert "verified" not in _concept(v02_bundle, "records/coastal-survey.md").frontmatter
        assert "verified" not in _concept(v02_bundle, "specimens/limpet.md").frontmatter

    def test_sources_entry_takes_its_resource_from_the_mirror(self, v02_bundle: OkfBundle):
        # The id is the page's own value under the knowledge base's declared `sources.key_field`; the
        # resource is the deep link that source's mirror carries, never a rebuilt URI.
        assert _concept(v02_bundle, "records/coastal-survey.md").frontmatter["sources"] == [
            {"id": "CAT-1", "resource": "catalogue://items/CAT-1", "title": "Coastal survey"}
        ]

    def test_sources_entry_omits_resource_when_the_mirror_has_no_link(self, v02_bundle: OkfBundle):
        # CAT-9 is catalogued but its mirror carries no deep link → id + title, no resource.
        assert _concept(v02_bundle, "records/dune-survey.md").frontmatter["sources"] == [{"id": "CAT-9", "title": "Dune survey"}]

    def test_no_sources_entry_for_a_page_naming_no_source(self, v02_bundle: OkfBundle):
        assert "sources" not in _concept(v02_bundle, "specimens/limpet.md").frontmatter

    def test_no_sources_entry_when_the_knowledge_base_declares_no_source_key(self, resolved: tuple[ResolvedNote, ...]):
        no_sources = _FIELDGUIDE_SPEC.sources.model_copy(update={"enabled": False, "key_field": ""})
        sourceless = _FIELDGUIDE_SPEC.model_copy(update={"sources": no_sources})

        bundle = project_bundle(resolved, collection="fieldguide", schema=sourceless, source_links=_SOURCE_LINKS)

        assert "sources" not in _concept(bundle, "records/coastal-survey.md").frontmatter

    def test_a_disabled_sources_block_carries_no_entry_even_with_its_field_names_left_declared(
        self, resolved: tuple[ResolvedNote, ...]
    ):
        # `enabled` is the switch; a spec that turned the layer off but kept its field names is off.
        disabled = _FIELDGUIDE_SPEC.model_copy(update={"sources": _FIELDGUIDE_SPEC.sources.model_copy(update={"enabled": False})})

        bundle = project_bundle(resolved, collection="fieldguide", schema=disabled, source_links=_SOURCE_LINKS)

        record = _concept(bundle, "records/coastal-survey.md")
        assert "sources" not in record.frontmatter
        assert "source_id" not in _engram(record)
        # And nothing is lost: the two field values ride the long tail instead.
        assert _fields(record)["catalogue_key"] == "CAT-1"
        assert _fields(record)["catalogue_hash"] == "bd72eebf"

    def test_default_actor_is_the_dev_fallback(self, bundle: OkfBundle):
        # Callers that pass no actor get the documented dev fallback; the handler always
        # passes the real installed-version actor.
        generated = _concept(bundle, "records/coastal-survey.md").frontmatter["generated"]
        assert isinstance(generated, dict)
        assert generated["by"] == "engram/dev"


# -- extension keys ------------------------------------------------------------


class TestExtensionKeys:
    def test_okf_x_relations_maps_typed_edges_to_resolved_concept_paths(self, bundle: OkfBundle):
        relations = _concept(bundle, "records/coastal-survey.md").frontmatter["okf_x_relations"]
        assert relations == {"describes": ["specimens/limpet"]}

    def test_okf_x_relations_excludes_edges_to_unexported_targets(self, bundle: OkfBundle):
        # follows → rec.dune-survey is dangling in this knowledge base → not in relations.
        assert "follows" not in _relations(_concept(bundle, "records/coastal-survey.md"))
        assert _relations(_concept(bundle, "specimens/limpet.md")) == {"resembles": ["specimens/periwinkle"]}

    def test_okf_x_engram_carries_id_status_collection_facets_and_aliases(self, bundle: OkfBundle):
        engram = _engram(_concept(bundle, "records/coastal-survey.md"))
        assert engram["id"] == "rec.coastal-survey"
        assert engram["status"] == "synthesized"
        assert engram["collection"] == "fieldguide"
        assert engram["habitats"] == ["shoreline"]
        assert engram["seasons"] == ["spring", "summer"]
        assert engram["aliases"] == ["The coastal survey"]

    def test_okf_x_engram_carries_the_source_key_and_hash_the_spec_names(self, bundle: OkfBundle):
        engram = _engram(_concept(bundle, "records/coastal-survey.md"))
        assert engram["source_id"] == "CAT-1"
        assert engram["source_hash"] == "bd72eebf"

    def test_okf_x_engram_omits_the_source_pair_for_a_page_naming_no_source(self, bundle: OkfBundle):
        engram = _engram(_concept(bundle, "specimens/limpet.md"))
        assert "source_id" not in engram
        assert "source_hash" not in engram

    def test_the_long_tail_rides_under_one_generic_key(self, bundle: OkfBundle):
        # Whatever else the knowledge base authored survives verbatim under `fields`, so no knowledge base's
        # own field names are declared here and nothing authored is dropped.
        fields = _fields(_concept(bundle, "records/coastal-survey.md"))
        assert fields["season_notes"] == "taken at low tide"
        assert fields["attachments"] == {"plates": "https://example.invalid/plates"}

    def test_the_long_tail_coerces_authored_dates_to_json(self, bundle: OkfBundle):
        # `created` parses to a date; the bundle's frontmatter is serialised, so it lands as its
        # ISO string rather than a Python object.
        assert _fields(_concept(bundle, "records/coastal-survey.md"))["created"] == "2026-06-01"

    def test_the_long_tail_excludes_what_the_projection_already_carries(self, bundle: OkfBundle):
        fields = _fields(_concept(bundle, "records/coastal-survey.md"))
        for key in ("tags", "updated", "habitats", "seasons", "catalogue_key", "catalogue_hash"):
            assert key not in fields

    def test_a_page_with_no_long_tail_carries_no_fields_key(self, bundle: OkfBundle):
        assert "fields" not in _engram(_concept(bundle, "specimens/periwinkle.md"))

    def test_a_facet_the_overlay_could_not_read_stays_in_the_long_tail(self):
        """Nothing authored is dropped: a key the overlay did not emit rides `fields` verbatim.

        `facet_values` reads strings, so a facet authored as a number yields no overlay entry. The
        value must then survive in the long tail rather than falling between the two.
        """
        numeric_facet = _RECORD.replace("seasons: [spring, summer]", "seasons: 1998")
        resolved_notes = _resolve([("Vault/Records/coastal-survey.md", numeric_facet)])

        bundle = project_bundle(resolved_notes, collection="fieldguide", schema=_FIELDGUIDE_SPEC)

        record = _concept(bundle, "records/coastal-survey.md")
        assert "seasons" not in _engram(record)  # not a string list, so the overlay emits nothing
        assert _fields(record)["seasons"] == 1998  # and it is kept here instead of dropped
        assert _engram(record)["habitats"] == ["shoreline"]  # the readable facet still lands in the overlay
        assert "habitats" not in _fields(record)  # and is not repeated


# -- body rewriting ------------------------------------------------------------


class TestBodyRewriting:
    def test_resolved_unit_link_becomes_relative_markdown_link(self, bundle: OkfBundle):
        body = _concept(bundle, "records/coastal-survey.md").body
        assert "[limpet](/specimens/limpet.md)" in body

    def test_dangling_unit_link_becomes_plain_text(self, bundle: OkfBundle):
        body = _concept(bundle, "records/coastal-survey.md").body
        assert "It never reached a missing place." in body
        assert "[[a missing place]]" not in body

    def test_aliased_unit_link_uses_alias_text(self, bundle: OkfBundle):
        body = _concept(bundle, "specimens/limpet.md").body
        assert "[coastal survey](/records/coastal-survey.md)" in body

    def test_unit_link_inside_code_fence_is_left_untouched(self, bundle: OkfBundle):
        body = _concept(bundle, "specimens/limpet.md").body
        assert "# a fenced block: [[periwinkle]] here must NOT be rewritten" in body
        assert 'count("[[limpet]]")' in body

    def test_relations_section_lists_each_exported_edge(self, bundle: OkfBundle):
        body = _concept(bundle, "records/coastal-survey.md").body
        assert "## Relations" in body
        assert "**describes**" in body
        assert "[Limpet](/specimens/limpet.md)" in body
        # The dangling `follows` target is not rendered as a relation.
        assert "dune-survey" not in body.split("## Relations", 1)[1]


# -- bundle index + conformance -------------------------------------------------


class TestBundleIndexAndConformance:
    def test_index_declares_okf_version_and_nothing_else(self, bundle: OkfBundle):
        assert bundle.index.path == "index.md"
        # v0.2 pins the reserved file's structure: the bundle-root index.md frontmatter
        # is the okf_version key alone (reserved files are exempt from the type rule).
        assert bundle.index.frontmatter == {"okf_version": "0.2"}

    def test_index_body_lists_concepts_grouped_by_subdir(self, bundle: OkfBundle):
        body = bundle.index.body
        assert "records/" in body
        assert "specimens/" in body
        # Each concept linked with its description where present.
        assert "[specimens/limpet](/specimens/limpet.md)" in body
        assert "A grazing marine snail." in body

    def test_every_concept_has_a_non_empty_type(self, bundle: OkfBundle):
        for concept in bundle.concepts:
            type_value = concept.frontmatter.get("type")
            assert isinstance(type_value, str)
            assert type_value  # non-empty — the one OKF-required conformance rule


# -- the subdir derivation ------------------------------------------------------


class TestSubdirDerivation:
    """A page_type's bundle subdirectory comes from the knowledge base's own spec, never a pinned table."""

    def test_a_kind_derives_its_subdir_from_its_declared_folder_basename(self):
        assert _subdir_for_page_type("record", _FIELDGUIDE_SPEC) == "records"
        assert _subdir_for_page_type("atlas", _FIELDGUIDE_SPEC) == "atlases"

    def test_a_kind_with_no_declared_folder_falls_back_to_the_page_type_name(self):
        spec = KnowledgeBaseSchema(
            version="0.1",
            slug="folderless",
            page_types=(PageTypeSpec(page_type="snippet", id_prefix="snip."),),  # folder=None
        )
        assert _subdir_for_page_type("snippet", spec) == "snippet"


# -- a second, differently-shaped knowledge base ----------------------------------------

_KITCHEN_SPEC = KnowledgeBaseSchema(
    version="0.1",
    slug="kitchen",
    page_types=(
        PageTypeSpec(page_type="recipe", id_prefix="rec.", folder="Book/Recipes"),
        PageTypeSpec(page_type="ingredient", id_prefix="ing.", folder="Book/Ingredients"),
    ),
)

_RECIPE = """\
---
id: rec.ratatouille
type: recipe
title: Ratatouille
summary: A classic Provençal dish.
status: draft
---

## Notes

Uses [[tomato]].
"""

_INGREDIENT = """\
---
id: ing.tomato
type: ingredient
title: Tomato
status: draft
---

## Notes

A red fruit used in many dishes.
"""


class TestASecondKnowledgeBase:
    """A knowledge base with other page_types, no facets and no source layer exports on the same rules."""

    def test_exports_every_kind_its_own_spec_declares(self):
        resolved_notes = _resolve(
            [("Book/Recipes/ratatouille.md", _RECIPE), ("Book/Ingredients/tomato.md", _INGREDIENT)], spec=_KITCHEN_SPEC
        )

        result = project_with_counts(resolved_notes, collection="kitchen", schema=_KITCHEN_SPEC)

        assert {concept.path for concept in result.bundle.concepts} == {"recipes/ratatouille.md", "ingredients/tomato.md"}
        assert result.skipped_meta == 0

    def test_carries_no_sources_and_no_source_pair_without_a_source_layer(self):
        resolved_notes = _resolve([("Book/Recipes/ratatouille.md", _RECIPE)], spec=_KITCHEN_SPEC)

        bundle = project_bundle(resolved_notes, collection="kitchen", schema=_KITCHEN_SPEC)

        concept = _concept(bundle, "recipes/ratatouille.md")
        assert "sources" not in concept.frontmatter
        assert "source_id" not in _engram(concept)
