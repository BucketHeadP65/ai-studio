"""The library's own knowledge base schema lives in this repository, and the library's write path reads it there by default."""

from __future__ import annotations

import os

from typing import get_args

import pytest

from core.ai.workflows.contracts import FlagKind
from engram.kb.contracts import KnowledgeBaseSchema

from harness import settings
from harness.settings import CHECKOUT, repo_path

SCHEMA = CHECKOUT / "library" / "knowledge-base-schema.json"


@pytest.fixture(scope="module")
def schema() -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema.from_path(SCHEMA)


def test_the_schema_declares_workflows_reusable_steps_and_maintenance_flags(schema: KnowledgeBaseSchema) -> None:
    assert [page_type.page_type for page_type in schema.page_types] == ["workflow", "step", "maintenance-flag"]
    assert schema.retrieval.collection == "workflows"


def test_a_maintenance_flag_opens_at_open_and_resolves_to_resolved(schema: KnowledgeBaseSchema) -> None:
    flag = schema.page_type("maintenance-flag")
    assert flag is not None
    assert flag.id_prefix == "mf."
    assert flag.status_values == ("open", "resolved")
    assert flag.decision is not None
    assert flag.decision.open == "open"
    assert [outcome.status for outcome in flag.decision.outcomes] == ["resolved"]


def test_a_flag_page_declares_every_field_and_section_a_flag_writes(schema: KnowledgeBaseSchema) -> None:
    flag = schema.page_type("maintenance-flag")
    assert flag is not None
    fields = {field.name: field for field in flag.frontmatter_fields}
    assert set(fields) == {"flag_kind", "step", "step_id", "step_hash", "workflow", "raised_by", "raised_at", "resolved_by", "decision_reason"}
    assert {name for name, field in fields.items() if field.required} == {"flag_kind", "step", "step_id", "step_hash", "raised_by", "raised_at"}
    assert fields["flag_kind"].choices == get_args(FlagKind), "the schema accepts exactly the kinds a flag takes"
    assert flag.canonical_section_slugs == ("evidence", "proposed_change")


def test_the_library_schema_defaults_to_the_repository_copy() -> None:
    assert repo_path("APP_WORKFLOW_KNOWLEDGE_BASE", CHECKOUT / "library", environ={}) == CHECKOUT / "library"


@pytest.mark.skipif("APP_WORKFLOW_KNOWLEDGE_BASE" in os.environ, reason="the environment names another library schema")
def test_the_process_reads_the_repository_copy_when_no_export_names_another() -> None:
    assert settings.WORKFLOW_KNOWLEDGE_BASE == CHECKOUT / "library"
    assert (settings.WORKFLOW_KNOWLEDGE_BASE / "knowledge-base-schema.json").is_file()
