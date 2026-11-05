"""Every save path records the same consumer record: the Save button, `save_workflow`, `adapt_workflow` and `harness import`.

Each path saves the same workflow, holding a copy of the reusable step `digest` at v3, into its own library over one
small collection, `FakeCollection` with `prune` on, which drops `None` and empty containers at every depth before it
stores, as engram's ingest does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.ai.workflows.contracts import (
    Briefing,
    ConsumerEntry,
    FromNode,
    FromParameter,
    LibraryCollection,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowPresentation,
    step_content_address,
)
from core.ai.workflows.infrastructure.library_collection import WORKFLOW_COLLECTION, collection_toolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.authoring.domain import Draft
from harness.features.authoring.service import save_draft
from harness.features.browsing.importing import import_path
from harness.settings import SelfAuthoringSettings
from tests.support.fakes.library_collection import FakeCollection

if TYPE_CHECKING:
    from pathlib import Path

TEXT = Shape(json_schema={"type": "string"})


def _digest(*lines: str) -> Briefing:
    return Briefing(name="digest", instructions="\n".join(lines), inputs={"source": TEXT}, output=TEXT)


DIGEST = {
    1: _digest("Read the whole source.", "Write one digest page.", "Quote nothing."),
    2: _digest("Read the whole source.", "Write one digest page.", "Quote every claim."),
    3: _digest("Read the whole source.", "Write one digest page with a section per claim.", "Quote every claim and name its section."),
}
"""Three saved versions of the reusable step `digest`, each its own content."""

EDITED = _digest("Read the whole source.", "Write one digest page.", "Quote every claim, briefly.")
"""A copy of `digest` that matches no saved version and is closest by line to v2."""

NEWER = _digest("Read the whole source twice.", "Write one digest page.", "Quote every claim.")
"""A later version of `digest` the library does not hold, whose content address a consumer declines."""

EXPECTED = [ConsumerEntry(step="digest", version=3, step_hash=step_content_address(DIGEST[3])).model_dump(mode="json")]
"""The consumer record every path writes: the copy matches `digest` v3."""

WORKFLOW_FILE = """---
title: Digest sources
description: Digest one source.
uses: [st.digest.v3]
workflow:
  name: digest-sources
  version: 1
  description: Digest one source.
  parameters:
    source: {description: the source, json_schema: {type: string}}
  nodes:
    - kind: run
      id: digest
      work: digest
      inputs: {source: {parameter: source}}
  output: {node: digest}
---

Use per source. Not for a batch.
"""


def _workflow(*inline: Briefing) -> Workflow:
    """The workflow `WORKFLOW_FILE` holds, with `inline` as its copies."""
    return Workflow(
        name="digest-sources",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
        inline=inline,
        nodes=(RunNode(id="digest", work="digest", inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="digest"),
    )


async def _never_run(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover (saving never runs a step)
    raise AssertionError


def _registry() -> StepRegistry:
    return StepRegistry(perform=_never_run)


async def _library(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 to v3."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    return library


def _port(library: LibraryCollection) -> WorkflowContract[None]:
    return WorkflowContract[None](
        source=collection_toolset(library), vocabulary=WORKFLOW_COLLECTION, expose=(), registry=_registry(), saved_by="agent:test"
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _spec_yaml(workflow: Workflow) -> str:
    return yaml.safe_dump(workflow.model_dump(mode="json"), sort_keys=False)


async def _through_the_save_button(tmp_path: Path) -> list[object]:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = await _library(store)
    draft = Draft(
        spec_yaml=_spec_yaml(_workflow(DIGEST[3])),
        title="Digest sources",
        summary="Digest one source.",
        applicability="Use per source. Not for a batch.",
        presentation=WorkflowPresentation(
            purpose="Digest one source.", produces="One digest page.", owner_participation="You read the page.", not_for="A batch of sources."
        ),
    )
    workflow_id = await save_draft(draft, library=library, registry=_registry(), self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    return store.written(workflow_id)["reusable_steps"]


async def _through_save_workflow() -> list[object]:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    said = await _port(await _library(store))._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    assert said.startswith("saved as wf.digest-sources.v1"), said
    return store.written("wf.digest-sources.v1")["reusable_steps"]


async def _through_adapt_workflow() -> list[object]:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    port = _port(await _library(store))
    await port._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    said = await port._adapt(
        _ctx(), "wf.digest-sources.v1", ops=[{"op": "set_description", "to": "Digest one source, briefly."}], applicability="Use per source. Not for a batch."
    )
    assert said.startswith("adapted: wf.digest-sources.v1 -> wf.digest-sources.v2"), said
    return store.written("wf.digest-sources.v2")["reusable_steps"]


async def _through_the_import(tmp_path: Path) -> list[object]:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = await _library(store)
    folder = tmp_path / "import"
    (folder / "workflows").mkdir(parents=True)
    (folder / "workflows" / "digest-sources.md").write_text(WORKFLOW_FILE, encoding="utf-8")
    written = await import_path(folder, library=library, registry=_registry(), by="test:import")
    assert written == ("wf.digest-sources.v1",)
    return store.written("wf.digest-sources.v1")["reusable_steps"]


async def test_every_save_path_records_the_same_reusable_steps(tmp_path: Path) -> None:
    recorded = {
        "the Save button": await _through_the_save_button(tmp_path),
        "save_workflow": await _through_save_workflow(),
        "adapt_workflow": await _through_adapt_workflow(),
        "harness import": await _through_the_import(tmp_path),
    }

    assert recorded == dict.fromkeys(recorded, EXPECTED)


async def test_no_save_path_writes_uses(tmp_path: Path) -> None:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    port = _port(await _library(store))
    await port._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    folder = tmp_path / "import"
    (folder / "workflows").mkdir(parents=True)
    (folder / "workflows" / "digest-sources.md").write_text(WORKFLOW_FILE.replace("version: 1", "version: 2"), encoding="utf-8")
    await import_path(folder, library=LibraryCollection(store, store.ingest, collection=store.collection), registry=_registry(), by="test:import")

    assert [("uses" in payload["frontmatter"]) for payload in store.payloads if payload["frontmatter"]["type"] == "workflow"] == [False, False, False]


async def test_importing_an_unchanged_version_again_keeps_its_decline_and_its_diverged_entry(tmp_path: Path) -> None:
    """`harness import` saves every workflow file on every run, with no lineage, and must not forget what the page recorded."""
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = await _library(store)
    port = _port(library)
    await port._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    await port._adapt(
        _ctx(),
        "wf.digest-sources.v1",
        ops=[{"op": "replace_inline_step", "name": "digest", "with": EDITED.model_dump(mode="json")}],
        applicability="Use per source. Not for a batch.",
    )
    await library.record_decline("wf.digest-sources.v2", step="digest", step_hash=step_content_address(NEWER), because="kept")
    kept = await library.fetch_consumer_record("wf.digest-sources.v2")
    assert kept == (
        ConsumerEntry(
            step="digest",
            version=None,
            step_hash=step_content_address(EDITED),
            diverged_from=3,
            diverged_basis="parent",
            declined=step_content_address(NEWER),
            declined_because="kept",
        ),
    )
    folder = tmp_path / "import"
    (folder / "workflows").mkdir(parents=True)
    document = {"title": "Digest sources", "description": "Digest one source.", "workflow": (await library.fetch_workflow("wf.digest-sources.v2")).model_dump(mode="json")}
    (folder / "workflows" / "digest-sources.md").write_text(f"---\n{yaml.safe_dump(document, sort_keys=False)}---\n\nUse per source. Not for a batch.\n", encoding="utf-8")

    assert await import_path(folder, library=library, registry=_registry(), by="test:import") == ("wf.digest-sources.v2",)

    assert await library.fetch_consumer_record("wf.digest-sources.v2") == kept
