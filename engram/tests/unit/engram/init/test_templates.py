"""Unit tests for `engram.init.infrastructure.templates`: template resolution by slug."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

from core.markdown import split_frontmatter

from engram.init.contracts import Template, list_templates, load_template, scaffold
from engram.init.infrastructure.templates import templates_root
from engram.kb.contracts import KnowledgeBaseSchema, parse_vocabulary, read_policy, schema_vocabulary_drift

if TYPE_CHECKING:
    from pathlib import Path

NEWS_OUTLETS = (
    "Advanced Science News",
    "BBC News - Entertainment & Arts",
    "BBC News - Technology",
    "Business Line",
    "Business Today | Latest Stock Market And Economy News India",
    "Business World",
    "CBSSports.com",
    "Cnbc | World Business News Leader",
    "Engadget",
    "Eos: Earth And Space Science News",
    "Essentially Sports",
    "FOX News - Entertainment",
    "FOX News - Health",
    "FOX News - Lifestyle",
    "Financial Times",
    "Fortune",
    "Globes English | Israel Business Arena",
    "Hacker News",
    "Insidesport",
    "Iot Business News",
    "Live Science: The Most Interesting Articles",
    "Mashable",
    "Music Business Worldwide",
    "Polygon",
    "Revyuh Media",
    "Rivals",
    "Science News For Students",
    "Scitechdaily | Science Space And Technology News 2017",
    "Seeking Alpha",
    "Sky Sports",
    "Sport Grill",
    "Sporting News",
    "Sportskeeda",
    "TalkSport",
    "TechCrunch",
    "The Age",
    "The Guardian",
    "The Independent - Life and Style",
    "The Independent - Sports",
    "The Independent - Travel",
    "The New York Times",
    "The Roar | Sports Writers Blog",
    "The Sydney Morning Herald",
    "The Verge",
    "Wide World Of Sports",
    "Wired",
    "Yahoo News",
    "Yardbarker",
    "Zee Business",
)
"""Every value of the MultiHop-RAG articles' `source` field, verbatim and sorted, as the dataset's `corpus.json` holds them."""


def _news_benchmark() -> tuple[Template, KnowledgeBaseSchema]:
    template = load_template("news-benchmark")
    return template, KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")


def _make_bundle_dir(root: Path, slug: str, *, readme: str | None = None, with_spec: bool = True) -> Path:
    """Build a fake template directory under `root`, with an optional README and knowledge-base-schema.json."""
    template_dir = root / slug
    template_dir.mkdir(parents=True)
    if with_spec:
        (template_dir / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    if readme is not None:
        (template_dir / "README.md").write_text(readme, encoding="utf-8")
    return template_dir


class TestBundlesRoot:
    """`templates_root` reads `ENGRAM_TEMPLATES` first, else walks up to the repo's `templates/`."""

    def test_environ_override_wins(self, tmp_path: Path) -> None:
        assert templates_root({"ENGRAM_TEMPLATES": str(tmp_path)}) == tmp_path

    def test_no_override_finds_the_repo_bundles_directory(self) -> None:
        root = templates_root({})
        assert root.is_dir()
        assert root.name == "templates"
        assert {p.name for p in root.iterdir()} >= {"research", "repo-docs"}


class TestListBundles:
    """`list_templates` lists every subdirectory of the root that carries its own `knowledge-base-schema.json`."""

    def test_empty_root_answers_no_bundles(self, tmp_path: Path) -> None:
        assert list_templates({"ENGRAM_TEMPLATES": str(tmp_path / "missing")}) == ()

    def test_only_directories_with_knowledge_base_schema_json_count(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "real", readme="# Real template\n")
        _make_bundle_dir(tmp_path, "no-spec", with_spec=False, readme="# Not a template\n")
        (tmp_path / "stray-file.md").write_text("not a directory", encoding="utf-8")

        templates = list_templates({"ENGRAM_TEMPLATES": str(tmp_path)})

        assert [b.slug for b in templates] == ["real"]

    def test_title_comes_from_the_readme_first_heading(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "research", readme="# Research knowledge base\n\nSome body text.\n")

        templates = list_templates({"ENGRAM_TEMPLATES": str(tmp_path)})

        assert templates == (Template(slug="research", title="Research knowledge base", path=tmp_path / "research"),)

    def test_title_falls_back_to_slug_without_a_readme_heading(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "no-heading", readme="Just a paragraph, no heading.\n")
        _make_bundle_dir(tmp_path, "no-readme", readme=None)

        templates = {b.slug: b.title for b in list_templates({"ENGRAM_TEMPLATES": str(tmp_path)})}

        assert templates == {"no-heading": "no-heading", "no-readme": "no-readme"}

    def test_sorted_by_slug(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "zeta", readme="# Zeta\n")
        _make_bundle_dir(tmp_path, "alpha", readme="# Alpha\n")

        templates = list_templates({"ENGRAM_TEMPLATES": str(tmp_path)})

        assert [b.slug for b in templates] == ["alpha", "zeta"]

    def test_the_two_shipped_bundles_resolve_from_the_real_repo(self) -> None:
        by_slug = {b.slug: b for b in list_templates({})}
        assert by_slug["research"].title == "research"
        assert by_slug["repo-docs"].title == "repo-docs"


class TestLoadBundle:
    """`load_template` resolves one template by slug, or raises naming the known slugs."""

    def test_loads_a_known_bundle(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "research", readme="# Research knowledge base\n")

        template = load_template("research", {"ENGRAM_TEMPLATES": str(tmp_path)})

        assert template == Template(slug="research", title="Research knowledge base", path=tmp_path / "research")

    def test_unknown_slug_names_the_known_bundles(self, tmp_path: Path) -> None:
        _make_bundle_dir(tmp_path, "research", readme="# Research knowledge base\n")
        _make_bundle_dir(tmp_path, "repo-docs", readme="# repo-docs\n")

        with pytest.raises(LookupError, match="research") as excinfo:
            load_template("nonexistent", {"ENGRAM_TEMPLATES": str(tmp_path)})
        assert "repo-docs" in str(excinfo.value)

    def test_unknown_slug_with_no_bundles_at_all(self, tmp_path: Path) -> None:
        with pytest.raises(LookupError, match="none"):
            load_template("anything", {"ENGRAM_TEMPLATES": str(tmp_path / "missing")})

    def test_loads_from_the_real_repo_bundles(self) -> None:
        template = load_template("research", {})
        assert template.slug == "research"
        assert (template.path / "knowledge-base-schema.json").is_file()


def test_the_benchmark_dataset_template_loads_and_reads_the_beir_connector():
    from engram.kb.contracts import KnowledgeBaseSchema

    template = load_template("benchmark-dataset")

    schema = KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")

    assert schema.sources.connectors == ("beir",)
    assert schema.sources.digest_types == {"article": "digest"}
    assert template.title == "Benchmark dataset"


def test_the_benchmark_dataset_template_digests_into_quoted_claims_through_its_own_workflow():
    from engram.kb.contracts import KnowledgeBaseSchema

    template = load_template("benchmark-dataset")

    schema = KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")

    assert schema.sources.digest_workflow == "benchmark-digest"
    assert schema.sources.quote_verify is True
    digest = schema.page_type("digest")
    assert digest is not None
    assert digest.claims_enabled
    assert schema.claims is not None
    assert schema.claims.section == "findings"
    assert set(schema.claims.plain_fields) == {"loc", "quote", "kind"}
    assert set(schema.claims.quote_required_types) == {"finding", "method"}
    assert schema.claims.edge_relations == ()
    assert schema.claims.multilink_fields == ()
    assert schema.claims.link_scalar_fields == ()
    assert schema.claims.text_scalar_fields == ()
    assert schema.claims.comparative_relations == ()
    assert {entry.value for entry in schema.claim_types} == {"finding", "method"}
    assert len(schema.claim_types) == 2
    assert schema.relations == ()
    digest_sources = schema.compounding.entry("digest-sources")
    assert digest_sources is not None
    assert digest_sources.to == ("digest",)
    assert digest_sources.cap_per_run == 40


def test_the_news_benchmark_template_reads_the_multihop_rag_connector_into_digest_fields() -> None:
    template, schema = _news_benchmark()

    assert template.title == "News benchmark"
    assert schema.slug == "news-benchmark"
    assert schema.retrieval.collection == "news-benchmark"
    assert schema.retrieval.profile == "full"
    assert schema.retrieval.doc_authoring is True
    assert schema.sources.connectors == ("multihop_rag",)
    assert schema.sources.digest_types == {"article": "digest"}
    assert dict(schema.sources.digest_fields) == {
        name: name for name in ("outlet", "author", "published_at", "published_on", "category", "url")
    }
    assert schema.sources.digest_claim_text_fields is True
    assert schema.sources.quote_verify is True
    assert schema.sources.digest_workflow == "news-digest"
    assert schema.run_indexing == "once_per_run"
    digest = schema.page_type("digest")
    assert digest is not None
    assert digest.id_prefix == "dg."
    assert digest.folder == "Wiki/Digests"
    assert digest.canonical_section_slugs == ("tl_dr", "findings")
    assert digest.claims_enabled
    assert [field.name for field in digest.frontmatter_fields if field.required] == ["digest_of"]


def test_the_news_benchmark_template_dates_each_page_by_its_publication_day() -> None:
    _, schema = _news_benchmark()

    assert schema.retrieval.date_field == "published_on"


@pytest.mark.parametrize("slug", ["benchmark-dataset", "repo-docs", "research"])
def test_every_other_template_dates_each_page_by_the_day_it_was_written(slug: str) -> None:
    template = load_template(slug)

    schema = KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")

    assert schema.retrieval.date_field == "created"


def test_the_news_benchmark_template_resolves_entities_and_relates_digests_through_its_own_fields() -> None:
    _, schema = _news_benchmark()

    assert schema.entities is not None
    assert schema.entities.page_type == "entity"
    entity = schema.page_type("entity")
    assert entity is not None
    assert entity.id_prefix == "ent."
    assert entity.folder == "Wiki/Entities"
    assert entity.canonical_section_slugs == ("tl_dr",)
    assert not entity.claims_enabled
    assert [(field.name, field.required) for field in entity.frontmatter_fields] == [("entity_type", True)]
    assert [entry.value for entry in schema.entity_types] == ["person", "organization", "product", "event", "place", "work"]
    relations = {relation.slug: (relation.domain, relation.range) for relation in schema.relations}
    assert relations == {"mentions": ("digest", "entity"), "follows_up": ("digest", "digest")}
    follows_up = next(relation for relation in schema.relations if relation.slug == "follows_up")
    assert "from the later digest to the earlier one" in follows_up.description
    assert "card" in follows_up.description, "the model judges direction by the date each card prints"
    digest = schema.page_type("digest")
    assert digest is not None
    assert digest.edge_fields == ("mentions", "follows_up")
    assert digest.link_list_edge_fields >= {"mentions", "follows_up"}


def test_the_news_benchmark_template_quotes_every_claim_kind_and_names_its_subject_speaker_date_and_figure() -> None:
    _, schema = _news_benchmark()

    assert schema.claims is not None
    assert schema.claims.section == "findings"
    assert schema.claims.link_scalar_fields == ("subject", "speaker")
    assert schema.claims.text_scalar_fields == ("when", "figure")
    assert set(schema.claims.plain_fields) == {"loc", "quote", "kind"}
    assert schema.claims.multilink_fields == ()
    assert schema.claims.edge_relations == ()
    assert [entry.value for entry in schema.claim_types] == ["action", "statement", "measurement"]
    assert set(schema.claims.quote_required_types) == {"action", "statement", "measurement"}


def test_the_news_benchmark_template_seeds_every_outlet_and_category_verbatim_on_digest_cards() -> None:
    _, schema = _news_benchmark()

    facets = {facet.key: facet for facet in schema.facets}
    assert list(facets) == ["outlet", "category", "published_on"], "the card prints the date after outlet and category"
    assert facets["outlet"].seed == NEWS_OUTLETS
    assert facets["category"].seed == ("business", "entertainment", "health", "science", "sports", "technology")
    assert facets["published_on"].seed == (), "any date is admitted"
    for facet in facets.values():
        assert facet.scope == ("digest",)
        assert facet.card is True
    digest = schema.page_type("digest")
    assert digest is not None
    published_on = next(field for field in digest.frontmatter_fields if field.name == "published_on")
    assert published_on.type == "scalar"


def test_the_news_benchmark_template_scaffolds_a_vocabulary_holding_every_outlet_and_category() -> None:
    """Five outlet names carry a pipe, so the facet table must keep each one whole for its digests to validate."""
    _, schema = _news_benchmark()

    files = scaffold(schema)
    documents = [(path.rsplit("/", 1)[-1], text) for path, text in files.items() if path.startswith("_meta/vocabulary/")]
    index = parse_vocabulary(documents).index

    assert tuple(index.facets["outlet"]) == NEWS_OUTLETS
    assert tuple(index.facets["category"]) == ("business", "entertainment", "health", "science", "sports", "technology")
    assert "published_on" not in index.facets, "an unseeded facet registers no values, so every date validates"
    assert schema_vocabulary_drift(index, schema) == ()


def test_the_news_benchmark_template_admits_only_the_dataset_s_articles_and_fetches_no_reference() -> None:
    template, schema = _news_benchmark()

    policy = read_policy(template.path, schema)

    assert policy.present
    assert policy.parse_error is None
    assert policy.unknown_rules == ()
    assert policy.rules.source_types == ("article",)
    assert policy.rules.hop_depth == 0
    assert policy.rules.per_source_cap == 1
    assert policy.rules == schema.sources.source_queue, "the page and the schema's fallback state one policy"
    assert policy.guidance


def test_the_news_benchmark_template_carries_its_documentation_and_compounding_policy_as_guidance() -> None:
    _, schema = _news_benchmark()

    digest = schema.page_type("digest")
    entity = schema.page_type("entity")
    assert digest is not None
    assert entity is not None
    assert digest.guidance
    assert all(section.guidance for section in digest.canonical_sections), "TL;DR and Findings each say what they hold"
    assert "resolve_entities" in entity.guidance
    assert all(section.guidance for section in entity.canonical_sections)
    digest_sources = schema.compounding.entry("digest-sources")
    assert digest_sources is not None
    assert digest_sources.to == ("digest",)
    assert digest_sources.cap_per_run == 50
    assert digest_sources.gate == "none"
    assert digest_sources.guidance


@pytest.mark.parametrize("template", list_templates({}), ids=lambda template: template.slug)
def test_every_meta_page_of_a_shipped_template_carries_frontmatter_that_parses(template: Template) -> None:
    pages = sorted((template.path / "_meta").rglob("*.md"))
    for page in pages:
        text = page.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        try:
            data, _ = split_frontmatter(text)
        except yaml.YAMLError as error:
            pytest.fail(f"{template.slug}/{page.relative_to(template.path)}: {error}")
        assert data, f"{template.slug}/{page.relative_to(template.path)} has a frontmatter fence but no mapping inside it"
