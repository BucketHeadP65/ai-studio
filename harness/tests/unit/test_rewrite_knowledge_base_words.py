"""The knowledge base vocabulary migration over the library: the words, the fingerprint key, the evidence re-key."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from core.ai.workflows.contracts import Workflow, spec_identity

from harness.migrations import rewrite_knowledge_base_words
from harness.settings import CHECKOUT

if TYPE_CHECKING:
    from pathlib import Path

OLD_WORDS = (("knowledge bases", "corpora"), ("knowledge base", "corpus"))
"""How the shipped text read before the wave, for building a page in the old shape from a real one."""


def _old(value: Any) -> Any:  # noqa: ANN401 -- one branch per JSON shape, mirroring `rewrite_knowledge_base_words.renamed`
    """`value` with every new spelling put back to the old one, so a real workflow reads as a saved one."""
    if isinstance(value, str):
        for new, old in OLD_WORDS:
            value = value.replace(new, old)
        for old, new in reversed(rewrite_knowledge_base_words.TEXT_RENAMES):
            value = value.replace(new, old)
        return value
    if isinstance(value, dict):
        return {key: _old(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_old(item) for item in value]
    return value


def _shipped_workflow() -> dict[str, Any]:
    """The source-remove workflow as this checkout ships it, straight off its file."""
    text = (CHECKOUT / "workflows" / "workflows" / "source-remove.md").read_text(encoding="utf-8")
    document = yaml.safe_load(text.split("---")[1])
    return dict(document["workflow"])


def _page(spec: dict[str, Any]) -> dict[str, Any]:
    """One library page's long tail: a workflow plus a fingerprint still keyed `leaves`."""
    return {"id": "wf.source-remove.v2", "spec": spec, "fingerprint": {"leaves": {"trace_removal": "abc"}, "agents": {}, "grants": "g", "tools": {}}}


class TestOnePage:
    def test_the_rewrite_lands_exactly_on_the_shipped_workflow(self):
        shipped = _shipped_workflow()
        page = _page(_old(shipped))

        migrated = rewrite_knowledge_base_words.migrated_document(page, "when a source leaves a corpus")

        assert migrated is not None
        assert migrated.now == spec_identity(Workflow.model_validate(shipped))
        assert migrated.applicability == "when a source leaves a knowledge base"

    def test_the_address_it_moved_from_is_the_one_the_page_had(self):
        page = _page(_old(_shipped_workflow()))
        before = spec_identity(Workflow.model_validate(page["spec"]))

        migrated = rewrite_knowledge_base_words.migrated_document(page, "")

        assert migrated is not None
        assert migrated.was == before
        assert rewrite_knowledge_base_words.addresses_of(page) == (migrated.was, migrated.now)

    def test_the_fingerprint_key_takes_its_new_name(self):
        migrated = rewrite_knowledge_base_words.migrated_document(_page(_old(_shipped_workflow())), "")

        assert migrated is not None
        assert sorted(migrated.document["fingerprint"]) == ["agents", "grants", "steps", "tools"]

    def test_a_program_keeps_a_local_name_the_prose_rule_would_have_taken(self):
        page = _page(_old(_shipped_workflow()))
        page["spec"]["inline"][0]["source"] = "corpus = 1\ncorpus"

        migrated = rewrite_knowledge_base_words.migrated_document(page, "")

        assert migrated is not None
        assert migrated.document["spec"]["inline"][0]["source"] == "corpus = 1\ncorpus"

    def test_a_page_already_in_the_new_words_is_left_alone(self):
        page = {"id": "wf.x.v1", "spec": _shipped_workflow(), "fingerprint": {"steps": {}, "agents": {}, "grants": "g", "tools": {}}}

        assert rewrite_knowledge_base_words.migrated_document(page, "already plain") is None

    def test_a_workflow_that_would_not_validate_is_refused_by_name(self):
        page = {"id": "wf.broken.v1", "spec": {"name": "broken", "description": "calls corpus_profile"}}

        with pytest.raises(ValueError, match=r"wf\.broken\.v1"):
            rewrite_knowledge_base_words.migrated_document(page, "")


class TestEvidence:
    def test_a_record_filed_under_the_old_address_follows_the_workflow(self, tmp_path: Path):
        log = tmp_path / "evidence.jsonl"
        log.write_text(json.dumps({"spec_hash": "old", "kind": "judged"}) + "\nnot json\n", encoding="utf-8")
        report = rewrite_knowledge_base_words.Report()

        rewrite_knowledge_base_words.rekey_evidence(log, {"old": "new"}, report=report)

        lines = log.read_text(encoding="utf-8").splitlines()
        assert report.evidence_rekeyed == 1
        assert json.loads(lines[0]) == {"spec_hash": "new", "kind": "judged"}
        assert lines[1] == "not json"

    def test_a_second_pass_finds_no_old_address(self, tmp_path: Path):
        log = tmp_path / "evidence.jsonl"
        log.write_text(json.dumps({"spec_hash": "old"}) + "\n", encoding="utf-8")
        report = rewrite_knowledge_base_words.Report()

        rewrite_knowledge_base_words.rekey_evidence(log, {"old": "new"}, report=report)
        rewrite_knowledge_base_words.rekey_evidence(log, {"old": "new"}, report=report)

        assert report.evidence_rekeyed == 1


class TestWorkflowKnowledgeBase:
    def test_the_schema_file_the_folder_and_the_files_all_move(self, tmp_path: Path):
        root = tmp_path / "vault"
        (root / "_meta" / "registry").mkdir(parents=True)
        (root / "_meta" / "registry" / "facets.md").write_text("# facets\n", encoding="utf-8")
        (root / "corpusspec.json").write_text(json.dumps({"slug": "vault", "page_types": [{"kind": "workflow"}]}), encoding="utf-8")
        (root / "_meta" / "tpl.md").write_text("fingerprint:\n  leaves:\n    a: b\n", encoding="utf-8")
        report = rewrite_knowledge_base_words.Report()

        rewrite_knowledge_base_words.migrate_knowledge_base(root, report=report)

        assert json.loads((root / "knowledge-base-schema.json").read_text(encoding="utf-8"))["page_types"] == [{"page_type": "workflow"}]
        assert (root / "_meta" / "vocabulary" / "facets.md").is_file()
        assert "  steps:\n" in (root / "_meta" / "tpl.md").read_text(encoding="utf-8")
        assert report.clean

    def test_a_second_run_finds_nothing(self, tmp_path: Path):
        root = tmp_path / "vault"
        (root / "_meta").mkdir(parents=True)
        (root / "corpusspec.json").write_text(json.dumps({"slug": "vault", "page_types": [{"kind": "workflow"}]}), encoding="utf-8")
        rewrite_knowledge_base_words.migrate_knowledge_base(root, report=rewrite_knowledge_base_words.Report())

        again = rewrite_knowledge_base_words.Report()
        rewrite_knowledge_base_words.migrate_knowledge_base(root, report=again)

        assert not again.touched
        assert again.lines() == ["nothing to migrate: every page, schema file and workflow file already reads the new words"]
