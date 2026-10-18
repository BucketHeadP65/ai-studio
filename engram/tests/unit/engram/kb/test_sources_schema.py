"""Unit tests for `SourcesSpec` (`src/engram/kb/domain/schema.py`): the `connectors` tuple, the source-queue policy and digest fields.

A JSON `connector: "<name>"` is the one-connector spelling of `connectors`. `SourcesSpec.connector`
is a read-only view of the first declared connector (an empty string when none is declared). There
is no default connector: a knowledge base with `sources.enabled` names at least one. The schema's
entity page type and run indexing settings are tested here too, beside the digest fields they ship with.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from engram.init.contracts import load_template
from engram.kb.contracts import KnowledgeBaseSchema, SourceQueuePolicy, SourcesSpec
from engram.testing.specs import research_spec

_FIELD_NAMES = {"key_field": "zotero_key", "digest_link_field": "digest_of", "hash_field": "source_hash"}
"""A valid `key_field`/`digest_link_field`/`hash_field` trio, spread into a payload that also
declares `enabled: True` -- every mirror field name test below that is not itself testing this
trio's own validation needs one."""


def test_the_one_connector_spelling_loads_as_a_one_connector_tuple() -> None:
    spec = SourcesSpec.model_validate(
        {"enabled": True, "quote_verify": True, "connector": "youtube", "digest_types": {"paper": "paper"}, **_FIELD_NAMES}
    )
    assert spec.connectors == ("youtube",)
    assert spec.connector == "youtube"


def test_several_providers_load_in_order() -> None:
    spec = SourcesSpec.model_validate(
        {"enabled": True, "connectors": ["zotero", "intake"], "digest_types": {"paper": "paper"}, **_FIELD_NAMES}
    )
    assert spec.connectors == ("zotero", "intake")
    assert spec.connector == "zotero"


def test_there_is_no_default_provider() -> None:
    spec = SourcesSpec()

    assert spec.connectors == ()
    assert spec.enabled is False


def test_a_disabled_knowledge_base_with_no_providers_is_valid() -> None:
    assert SourcesSpec(enabled=False).connectors == ()


@pytest.mark.parametrize("bad", [[], ["zotero", "zotero"]])
def test_empty_or_repeated_providers_are_refused_when_enabled(bad: list[str]) -> None:
    with pytest.raises(ValueError, match="connectors"):
        SourcesSpec.model_validate({"enabled": True, "connectors": bad})


def test_repeated_providers_are_refused_even_when_disabled() -> None:
    with pytest.raises(ValueError, match="repeats"):
        SourcesSpec.model_validate({"connectors": ["zotero", "zotero"]})


def test_a_disabled_knowledge_base_leaves_the_mirror_field_names_empty_by_default() -> None:
    spec = SourcesSpec()

    assert spec.key_field == ""
    assert spec.digest_link_field == ""
    assert spec.hash_field == ""


@pytest.mark.parametrize("missing", ["key_field", "digest_link_field", "hash_field"])
def test_enabled_is_refused_when_one_mirror_field_name_is_missing(missing: str) -> None:
    payload = {"enabled": True, "connectors": ["zotero"], **{k: v for k, v in _FIELD_NAMES.items() if k != missing}}
    with pytest.raises(ValueError, match=missing):
        SourcesSpec.model_validate(payload)


def test_enabled_with_every_mirror_field_name_declared_is_valid() -> None:
    spec = SourcesSpec.model_validate({"enabled": True, "connectors": ["zotero"], "digest_types": {"paper": "paper"}, **_FIELD_NAMES})

    assert spec.key_field == "zotero_key"
    assert spec.digest_link_field == "digest_of"
    assert spec.hash_field == "source_hash"


@pytest.mark.parametrize("bad", ["Zotero_Key", "zotero-key", "1key", "key field"])
def test_a_disabled_knowledge_base_still_refuses_a_malformed_mirror_field_name(bad: str) -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        SourcesSpec.model_validate({"key_field": bad})


def test_digest_sections_over_defaults_to_unset() -> None:
    assert SourcesSpec().digest_sections_over is None


def test_digest_sections_over_accepts_a_positive_threshold() -> None:
    assert SourcesSpec.model_validate({"digest_sections_over": 40, "digest_part_field": "part_of"}).digest_sections_over == 40


def test_digest_part_field_defaults_to_empty() -> None:
    assert SourcesSpec().digest_part_field == ""


def test_a_split_threshold_with_no_part_field_is_refused_naming_both_settings() -> None:
    with pytest.raises(ValueError, match=r"sources\.digest_sections_over.*sources\.digest_part_field"):
        SourcesSpec.model_validate({"digest_sections_over": 40})


@pytest.mark.parametrize("bad", ["Part_Of", "part-of", "1part", "part of"])
def test_a_malformed_part_field_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="lowercase identifier"):
        SourcesSpec.model_validate({"digest_part_field": bad})


def _research_with_sources(**sources: object) -> dict[str, object]:
    payload = research_spec().model_dump(mode="json")
    sources_payload = payload["sources"]
    assert isinstance(sources_payload, dict)
    return {**payload, "sources": {**sources_payload, **sources}}


def test_the_research_template_names_part_of_as_its_part_field() -> None:
    assert research_spec().sources.digest_part_field == "part_of"


def test_a_split_threshold_with_a_part_field_every_digest_type_declares_is_valid() -> None:
    spec = KnowledgeBaseSchema.model_validate(_research_with_sources(digest_sections_over=40, digest_part_field="part_of"))

    assert spec.sources.digest_part_field == "part_of"


def test_a_part_field_a_digest_type_does_not_declare_is_refused_naming_both_settings() -> None:
    with pytest.raises(ValueError, match=r"sources\.digest_part_field 'lead_of'.*sources\.digest_types") as refused:
        KnowledgeBaseSchema.model_validate(_research_with_sources(digest_sections_over=40, digest_part_field="lead_of"))

    for page_type in ("article", "paper", "post", "talk"):
        assert page_type in str(refused.value)


@pytest.mark.parametrize("bad", [0, -1])
def test_digest_sections_over_refuses_a_non_positive_threshold(bad: int) -> None:
    with pytest.raises(ValueError, match="digest_sections_over"):
        SourcesSpec.model_validate({"digest_sections_over": bad})


def test_librarian_defaults_to_off() -> None:
    assert SourcesSpec().librarian is False


def test_librarian_can_be_turned_on() -> None:
    assert SourcesSpec.model_validate({"librarian": True}).librarian is True


def test_digest_types_default_to_empty_and_accept_a_declared_mapping() -> None:
    assert SourcesSpec().digest_types == {}
    narrowed = SourcesSpec.model_validate({"enabled": True, "connectors": ["zotero"], "digest_types": {"paper": "paper"}, **_FIELD_NAMES})
    assert narrowed.digest_types == {"paper": "paper"}


def test_enabled_with_no_digest_types_declared_is_refused() -> None:
    with pytest.raises(ValueError, match=r"sources\.digest_types"):
        SourcesSpec.model_validate({"enabled": True, "connectors": ["zotero"], **_FIELD_NAMES})


def test_enabled_with_digest_types_declared_is_valid() -> None:
    spec = SourcesSpec.model_validate({"enabled": True, "connectors": ["zotero"], "digest_types": {"paper": "paper"}, **_FIELD_NAMES})
    assert spec.digest_types == {"paper": "paper"}


def test_disabled_with_no_digest_types_declared_is_valid() -> None:
    spec = SourcesSpec.model_validate({"enabled": False})
    assert spec.digest_types == {}


def test_a_knowledge_base_that_declares_no_source_queue_policy_gets_the_defaults() -> None:
    policy = research_spec().sources.source_queue

    assert policy == SourceQueuePolicy()
    assert policy.source_types == ("video", "paper", "post", "article")
    assert policy.max_age_years is None
    assert policy.hop_depth == 1
    assert policy.per_source_cap == 5
    assert policy.never == ("profile", "encyclopedia", "homepage", "social-post", "repository")


def test_source_queue_rules_override_field_by_field() -> None:
    spec = SourcesSpec.model_validate({"source_queue": {"max_age_years": 6, "source_types": ["paper", "article"]}})

    assert spec.source_queue.max_age_years == 6
    assert spec.source_queue.source_types == ("paper", "article")
    assert spec.source_queue.per_source_cap == 5


def test_source_queue_rules_are_closed_sets_and_bounded() -> None:
    with pytest.raises(ValidationError, match="never"):
        SourcesSpec.model_validate({"source_queue": {"never": ["forum"]}})
    with pytest.raises(ValidationError, match="page_types"):
        SourcesSpec.model_validate({"source_queue": {"page_types": ["talk"]}})
    with pytest.raises(ValidationError, match="hop_depth"):
        SourcesSpec.model_validate({"source_queue": {"hop_depth": -1}})
    with pytest.raises(ValidationError, match="per_source_cap"):
        SourcesSpec.model_validate({"source_queue": {"per_source_cap": 0}})
    with pytest.raises(ValidationError, match="max_age_years"):
        SourcesSpec.model_validate({"source_queue": {"max_age_years": 0}})


def test_source_queue_policy_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SourceQueuePolicy.model_validate({"bogus": 1})


def test_digest_fields_default_to_empty_and_claim_text_fields_default_to_off() -> None:
    spec = SourcesSpec()

    assert spec.digest_fields == {}
    assert spec.digest_claim_text_fields is False


def test_digest_fields_and_claim_text_fields_load_as_declared() -> None:
    spec = SourcesSpec.model_validate({"digest_fields": {"published_at": "published_at"}, "digest_claim_text_fields": True})

    assert spec.digest_fields == {"published_at": "published_at"}
    assert spec.digest_claim_text_fields is True


@pytest.mark.parametrize(
    ("digest_fields", "bad"),
    [
        ({"Region": "region"}, "Region"),
        ({"published-at": "published_at"}, "published-at"),
        ({"region": "1region"}, "1region"),
        ({"region": "reg ion"}, "reg ion"),
    ],
)
def test_a_digest_field_name_that_is_not_a_lowercase_identifier_is_refused_naming_it(digest_fields: dict[str, str], bad: str) -> None:
    with pytest.raises(ValidationError, match="lowercase identifier") as refused:
        SourcesSpec.model_validate({"digest_fields": digest_fields})

    assert repr(bad) in refused.value.errors()[0]["msg"]


def test_a_digest_field_every_digest_type_declares_is_valid() -> None:
    spec = KnowledgeBaseSchema.model_validate(_research_with_sources(digest_fields={"part": "part_of"}))

    assert spec.sources.digest_fields == {"part": "part_of"}


def test_a_digest_field_no_digest_type_declares_is_refused_naming_the_field_and_every_page_type() -> None:
    with pytest.raises(ValidationError, match=r"sources\.digest_fields maps to 'region'") as refused:
        KnowledgeBaseSchema.model_validate(_research_with_sources(digest_fields={"region": "region"}))

    message = refused.value.errors()[0]["msg"]
    for page_type in ("article", "paper", "post", "talk"):
        assert page_type in message


def test_a_digest_field_one_digest_type_lacks_is_refused_naming_only_that_page_type() -> None:
    with pytest.raises(ValidationError, match=r"sources\.digest_fields maps to 'channel'") as refused:
        KnowledgeBaseSchema.model_validate(_research_with_sources(digest_fields={"channel": "channel"}))

    message = refused.value.errors()[0]["msg"]
    assert "paper" in message
    for page_type in ("article", "post", "talk"):
        assert page_type not in message


def _with_entity_page_type(**extra: object) -> dict[str, object]:
    return {
        "slug": "entities-test",
        "page_types": [
            {"page_type": "digest", "id_prefix": "dg"},
            {"page_type": "entity", "id_prefix": "ent", "frontmatter_fields": [{"name": "entity_type", "type": "scalar"}]},
        ],
        **extra,
    }


def test_a_schema_with_no_entities_block_has_no_entity_page_type() -> None:
    assert KnowledgeBaseSchema.model_validate(_with_entity_page_type()).entities is None


def test_an_entity_page_type_that_declares_entity_type_is_valid() -> None:
    spec = KnowledgeBaseSchema.model_validate(_with_entity_page_type(entities={"page_type": "entity"}))

    assert spec.entities is not None
    assert spec.entities.page_type == "entity"


def test_an_entity_page_type_that_is_not_declared_is_refused_naming_it() -> None:
    with pytest.raises(ValidationError, match=r"entities\.page_type 'person' is not a declared page type"):
        KnowledgeBaseSchema.model_validate(_with_entity_page_type(entities={"page_type": "person"}))


def test_an_entity_page_type_without_an_entity_type_field_is_refused_naming_it() -> None:
    with pytest.raises(ValidationError, match=r"entities\.page_type 'digest' does not declare the frontmatter field 'entity_type'"):
        KnowledgeBaseSchema.model_validate(_with_entity_page_type(entities={"page_type": "digest"}))


def test_the_entities_block_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match="bogus"):
        KnowledgeBaseSchema.model_validate(_with_entity_page_type(entities={"page_type": "entity", "bogus": 1}))


def test_run_indexing_defaults_to_each_write() -> None:
    assert KnowledgeBaseSchema.model_validate(_with_entity_page_type()).run_indexing == "each_write"


@pytest.mark.parametrize("value", ["each_write", "once_per_run"])
def test_run_indexing_accepts_its_two_values(value: str) -> None:
    assert KnowledgeBaseSchema.model_validate(_with_entity_page_type(run_indexing=value)).run_indexing == value


@pytest.mark.parametrize("value", ["never", "once", ""])
def test_run_indexing_refuses_any_other_value(value: str) -> None:
    with pytest.raises(ValidationError, match="run_indexing"):
        KnowledgeBaseSchema.model_validate(_with_entity_page_type(run_indexing=value))


@pytest.mark.parametrize("slug", ["research", "repo-docs", "benchmark-dataset"])
def test_a_shipped_template_still_loads_with_every_new_setting_at_its_default(slug: str) -> None:
    template = load_template(slug)

    spec = KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json")

    assert spec.sources.digest_fields == {}
    assert spec.sources.digest_claim_text_fields is False
    assert spec.entities is None
    assert spec.run_indexing == "each_write"
