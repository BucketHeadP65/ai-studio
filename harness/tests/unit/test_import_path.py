"""`import_path`: the file walking, the write ordering, and the pages the store actually receives.

`test_workflow_files_validate.py` proves the shipped artifacts validate; `test_import_cli.py` proves the CLI
wiring calls the right function. Neither runs `import_path` itself against a store -- this does,
over hand-written fakes of `LibraryCollection`'s two ports (mirrors `harness/tests/unit/
test_proof_reuse.py`'s `_FakeRows`/`_FakeIngest`, which mirror core's own).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    DocumentError,
    LibraryCollection,
    PresentationRefusedError,
    Program,
    SaveRefusedError,
    StepRegistry,
    WorkflowPresentation,
    fingerprint,
    load_spec_document,
    load_step_document,
)

from harness.features.browsing.importing import import_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

STEP_A = """---
title: Step A
summary: Says a.
version: 1
step:
  name: step_a
  source: |
    "a"
  output: {json_schema: {type: string}}
---

Use first.
"""

STEP_B = """---
title: Step B
summary: Appends b.
version: 1
step:
  name: step_b
  source: |
    prefix + "b"
  inputs:
    prefix: {json_schema: {type: string}}
  output: {json_schema: {type: string}}
---

Use second, on step_a's output.
"""

WORKFLOW = """---
title: Two step workflow
description: Runs step_a then step_b on its output.
uses: [st.step_a.v1, st.step_b.v1]
workflow:
  name: two-step
  version: 1
  description: Runs step_a then step_b on its output.
  nodes:
    - kind: run
      id: a
      work: step_a
    - kind: run
      id: b
      work: step_b
      inputs: {prefix: {node: a}}
  output: {node: b}
---

Use for the test.
"""

BROKEN_WORKFLOW = """---
title: Broken workflow
description: Names work nothing here registers or brings.
uses: []
workflow:
  name: broken
  version: 1
  description: Names work nothing here registers or brings.
  nodes:
    - kind: run
      id: a
      work: nowhere_registered
  output: {node: a}
---

Never saves.
"""

BROKEN_STEP = """---
title: Broken step
summary: No step key at all.
version: 1
---

Refused before it is even a step.
"""


class _FakeRows:
    """The two asyncpg calls `LibraryCollection`'s read side needs, answered from a script."""

    def __init__(self, *, rows: Sequence[Mapping[str, Any]] = (), value: str | Mapping[str, Any] | None = None) -> None:
        self.rows = rows
        self.value = value

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        del sql, args
        return self.rows

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        del sql, args
        return self.value


class _FakeIngest:
    """Records every payload `import_path` hands the store, in the order it hands them over."""

    def __init__(self) -> None:
        self.payloads: list[tuple[Mapping[str, Any], str]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        self.payloads.append((payload, collection))


def _registry() -> StepRegistry:
    """A registry that can build a `Program` step but registers no steps of its own."""

    async def run_code(step: Program, output_type: type, /, **inputs: object) -> object:
        raise AssertionError

    return StepRegistry(run_code=run_code)


def _write(path: Path, *, steps: dict[str, str], workflows: dict[str, str]) -> None:
    (path / "steps").mkdir()
    (path / "workflows").mkdir()
    for name, text in steps.items():
        (path / "steps" / name).write_text(text, encoding="utf-8")
    for name, text in workflows.items():
        (path / "workflows" / name).write_text(text, encoding="utf-8")


async def test_save_path_writes_steps_before_the_workflow_that_uses_them(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    written = await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert written == ("st.step_a.v1", "st.step_b.v1", "wf.two-step.v1")
    assert len(ingest.payloads) == 3
    types = [payload["frontmatter"]["type"] for payload, _ in ingest.payloads]
    assert types == ["step", "step", "workflow"]
    for payload, collection in ingest.payloads:
        assert collection == "test-knowledge-base"
        assert payload["frontmatter"]["saved_by"] == "test:save"
    workflow_frontmatter = ingest.payloads[2][0]["frontmatter"]
    assert workflow_frontmatter["id"] == "wf.two-step.v1"
    assert workflow_frontmatter["uses"] == ["st.step_a.v1", "st.step_b.v1"]


async def test_a_workflow_that_fails_validation_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A}, workflows={"broken.md": BROKEN_WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(SaveRefusedError):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert ingest.payloads == []


async def test_a_second_broken_step_file_means_nothing_is_written(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-broken.md": BROKEN_STEP}, workflows={})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(DocumentError, match=r"step-broken\.md"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert ingest.payloads == []


async def test_a_broken_workflow_file_is_refused_by_name(tmp_path: Path) -> None:
    _write(tmp_path, steps={}, workflows={"no-frontmatter.md": "just a body, no fence at all\n"})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(DocumentError, match=r"no-frontmatter\.md"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert ingest.payloads == []


async def test_a_saved_workflow_carries_the_fingerprint_the_contract_port_would_write(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")
    registry = _registry()
    environment = {"model": "some-model", "promoted_capabilities": "abc123"}

    await import_path(tmp_path, library=store, registry=registry, by="test:save", environment=environment)

    saved = ingest.payloads[2][0]["frontmatter"]["fingerprint"]
    # The port stamps `fingerprint(spec, registry, environment=...)` over the spec it is about to write:
    # the shipped file plus its `uses` steps, the same shape `import_path` assembled.
    document = load_spec_document(WORKFLOW)
    steps = [load_step_document(text).step for text in (STEP_A, STEP_B)]
    expected = fingerprint(document.workflow.model_copy(update={"inline": (*document.workflow.inline, *steps)}), registry, environment=environment)
    assert saved == expected.model_dump(mode="json")


async def test_an_import_with_no_environment_still_stamps_the_registry(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    stamp = ingest.payloads[2][0]["frontmatter"]["fingerprint"]
    assert stamp["environment"] == {}
    assert "grants" in stamp


PRESENTED_WORKFLOW = """---
title: Two step workflow
description: Runs step_a then step_b on its output.
uses: [st.step_a.v1, st.step_b.v1]
presentation:
  purpose: Turn one letter into two.
  produces: The two letters, in order.
  owner_participation: You read the answer.
  not_for: Anything with more than two letters.
  inputs:
    - {key: prefix, label: The letter to start from}
  result_fields:
    - {label: Letters, path: values.b}
  actions:
    - {kind: save_note, id: keep_it, label: Save the answer, artifact_id: letters}
workflow:
  name: two-step
  version: 1
  description: Runs step_a then step_b on its output.
  parameters:
    prefix:
      description: The letter to start from.
      json_schema: {type: string}
  result:
    summary: The letters the run produced.
    primary_work: b
    artifacts:
      - {id: letters, label: The letters, source: {node: b}, media_type: text/plain}
  nodes:
    - kind: run
      id: a
      work: step_a
    - kind: run
      id: b
      work: step_b
      inputs: {prefix: {node: a}}
  output: {node: b}
---

Use for the test.
"""

UNDECLARED_PRESENTATION = PRESENTED_WORKFLOW.replace("{key: prefix, label: The letter to start from}", "{key: quarter, label: Which quarter}")


async def test_an_imported_workflow_carries_the_presentation_its_file_declared(tmp_path: Path) -> None:
    """The template's own words travel into the library; nothing is inferred from the description."""
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": PRESENTED_WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    saved = ingest.payloads[2][0]["frontmatter"]["presentation"]
    declared = WorkflowPresentation.model_validate(load_spec_document(PRESENTED_WORKFLOW).frontmatter["presentation"])
    assert saved == declared.model_dump(mode="json")
    assert saved["purpose"] == "Turn one letter into two.", "the file's own words, not the spec's description"


async def test_a_workflow_file_with_no_presentation_writes_none(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert "presentation" not in ingest.payloads[2][0]["frontmatter"]


async def test_a_presentation_naming_an_undeclared_input_refuses_the_whole_folder(tmp_path: Path) -> None:
    """The same rule every other bad file follows: nothing at all is written, steps included."""
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": UNDECLARED_PRESENTATION})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(PresentationRefusedError, match="quarter"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save")

    assert ingest.payloads == []
