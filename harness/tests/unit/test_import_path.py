"""`import_path`: the file walking, the write ordering, and the pages the store actually receives.

`test_workflow_files_validate.py` proves the shipped artifacts validate; `test_import_cli.py` proves the CLI
wiring calls the right function. Neither runs `import_path` itself against a store -- this does,
over hand-written fakes of `LibraryCollection`'s two ports (mirrors `harness/tests/unit/
test_proof_reuse.py`'s `_FakeRows`/`_FakeIngest`, which mirror core's own).
"""

from __future__ import annotations

import json
import shutil

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from core.ai.workflows.contracts import (
    STEP_MAINTENANCE_AGENT,
    DocumentError,
    FromNode,
    InlineStep,
    LibraryCollection,
    Lineage,
    LiveTrialMarker,
    ParameterSpec,
    PresentationRefusedError,
    Program,
    ReusableStepSaved,
    RunNode,
    SaveRefusedError,
    StepRegistry,
    StepVersionChangedError,
    Workflow,
    WorkflowFingerprint,
    WorkflowPresentation,
    fingerprint,
    load_spec_document,
    load_step_document,
    step_content_address,
    step_page_id,
)
from pydantic import TypeAdapter

from harness.features.browsing.exceptions import (
    WorkflowCardChangedError,
    WorkflowNotLiveError,
    WorkflowVersionChangedError,
    WorkflowVersionSupersededError,
)
from harness.features.browsing.importing import FolderImport, SkippedWorkflowFile, import_path
from harness.features.evals.contracts import check_step_cases
from tests.support.fakes.library_collection import FakeCollection, pruned

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.cqrs.contracts import Event

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
    """Records every payload `import_path` hands the store, in the order it hands them over, and each event beside its page id."""

    def __init__(self) -> None:
        self.payloads: list[tuple[Mapping[str, Any], str]] = []
        self.events: list[tuple[str, Event]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str, events: Sequence[Event] = ()) -> None:
        self.payloads.append((payload, collection))
        self.events.extend((str(payload["frontmatter"]["id"]), event) for event in events)


def _registry(*, grants: Sequence[str] = ()) -> StepRegistry:
    """A registry that can build a `Program` step, granting `grants`, but registers no steps of its own."""

    async def run_code(step: Program, output_type: type, /, **inputs: object) -> object:
        raise AssertionError

    return StepRegistry(run_code=run_code, grants=grants)


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

    imported = await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported == FolderImport(written=("st.step_a.v1", "st.step_b.v1", "wf.two-step.v1"), skipped=())
    assert len(ingest.payloads) == 3
    types = [payload["frontmatter"]["type"] for payload, _ in ingest.payloads]
    assert types == ["step", "step", "workflow"]
    for payload, collection in ingest.payloads:
        assert collection == "test-knowledge-base"
        assert payload["frontmatter"]["saved_by"] == "test:save"
    workflow_frontmatter = ingest.payloads[2][0]["frontmatter"]
    assert workflow_frontmatter["id"] == "wf.two-step.v1"
    assert "uses" not in workflow_frontmatter, "the copies are recorded under reusable_steps (test_save_paths_record_consumers.py)"


async def test_an_import_of_new_step_versions_hands_one_event_per_step_and_none_with_the_workflow(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert [page for page, _ in ingest.events] == ["st.step_a.v1", "st.step_b.v1"], "the workflow's page carries no event"
    assert [(page, event.step_id, event.version, event.saved_by) for page, event in ingest.events if isinstance(event, ReusableStepSaved)] == [
        ("st.step_a.v1", "st.step_a.v1", 1, "test:save"),
        ("st.step_b.v1", "st.step_b.v1", 1, "test:save"),
    ]


async def test_a_workflow_that_fails_validation_writes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A}, workflows={"broken.md": BROKEN_WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(SaveRefusedError):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert ingest.payloads == []


async def test_a_second_broken_step_file_means_nothing_is_written(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-broken.md": BROKEN_STEP}, workflows={})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(DocumentError, match=r"step-broken\.md"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert ingest.payloads == []


async def test_a_broken_workflow_file_is_refused_by_name(tmp_path: Path) -> None:
    _write(tmp_path, steps={}, workflows={"no-frontmatter.md": "just a body, no fence at all\n"})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(DocumentError, match=r"no-frontmatter\.md"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert ingest.payloads == []


async def test_a_saved_workflow_carries_the_fingerprint_the_contract_port_would_write(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")
    registry = _registry()
    environment = {"model": "some-model", "promoted_capabilities": "abc123"}

    await import_path(tmp_path, library=store, registry=registry, by="test:save", check_cases=check_step_cases, environment=environment, moved_past_by=STEP_MAINTENANCE_AGENT)

    saved = ingest.payloads[2][0]["frontmatter"]["fingerprint"]
    # The port stamps `fingerprint(spec, registry, environment=...)` over the spec it is about to write:
    # the shipped file plus its `uses` steps, the same shape `import_path` assembled.
    document = load_spec_document(WORKFLOW)
    steps = [load_step_document(text).step for text in (STEP_A, STEP_B)]
    expected = fingerprint(document.workflow.model_copy(update={"inline": (*document.workflow.inline, *steps)}), registry, environment=environment)
    assert saved == expected.model_dump(mode="json")


STEP_A_ASKING_FOR_FETCH = STEP_A.replace("  name: step_a\n", "  name: step_a\n  tools: [fetch]\n")


async def test_an_import_with_no_environment_still_stamps_the_tools_its_steps_ask_for(tmp_path: Path) -> None:
    """The library drops empty containers, so with no host stamp the tools are what keeps the stamp stored."""
    _write(tmp_path, steps={"step-a.md": STEP_A_ASKING_FOR_FETCH, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(
        tmp_path, library=store, registry=_registry(grants=("fetch", "search")), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT
    )

    assert pruned(ingest.payloads[2][0]["frontmatter"]["fingerprint"]) == {"grants": ["fetch"]}


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

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    saved = ingest.payloads[2][0]["frontmatter"]["presentation"]
    declared = WorkflowPresentation.model_validate(load_spec_document(PRESENTED_WORKFLOW).frontmatter["presentation"])
    assert saved == declared.model_dump(mode="json")
    assert saved["purpose"] == "Turn one letter into two.", "the file's own words, not the spec's description"


async def test_a_workflow_file_with_no_presentation_writes_none(tmp_path: Path) -> None:
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": WORKFLOW})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert "presentation" not in ingest.payloads[2][0]["frontmatter"]


async def test_a_presentation_naming_an_undeclared_input_refuses_the_whole_folder(tmp_path: Path) -> None:
    """The same rule every other bad file follows: nothing at all is written, steps included."""
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={"two-step.md": UNDECLARED_PRESENTATION})
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    with pytest.raises(PresentationRefusedError, match="quarter"):
        await import_path(tmp_path, library=store, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert ingest.payloads == []


REPO = Path(__file__).resolve().parents[3]

CHANGED_STEP_B = STEP_B.replace('prefix + "b"', 'prefix + "bb"')

STEP_A_WITH_TOOLS = STEP_A.replace("  name: step_a\n", "  name: step_a\n  tools: []\n")

STEP_B_WITH_NULL_DEFAULT = STEP_B.replace("prefix: {json_schema: {type: string}}", "prefix: {json_schema: {type: string, default: null}}")

CASES = {"cases": [{"name": "says a", "inputs": {}, "expected_output": "a"}], "evaluators": ["EqualsExpected"]}


async def _holding(*texts: str) -> tuple[FakeCollection, LibraryCollection]:
    """A library over one fake collection that stores pages as engram does, holding each step file's version."""
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for text in texts:
        document = load_step_document(text)
        await library.put_step(
            document.step,
            version=document.version,
            title=document.title,
            summary=document.summary,
            applicability=document.applicability,
            provenance={"saved_by": "test:seed"},
            cases=CASES if document.step.name == "step_a" else None,
        )
    return store, library


def _write_cases(path: Path, name: str, cases: object) -> None:
    (path / "evals").mkdir(exist_ok=True)
    (path / "evals" / name).write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")


async def test_a_step_changed_at_an_unchanged_version_refuses_the_folder_before_its_first_write(tmp_path: Path) -> None:
    """step-b.md sorts after step-a.md, so only a check of every step before the first write keeps step_a unwritten."""
    store, library = await _holding(STEP_B)
    written = len(store.payloads)
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": CHANGED_STEP_B}, workflows={})

    with pytest.raises(StepVersionChangedError, match=r"st\.step_b\.v1") as refused:
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert refused.value.step_id == "st.step_b.v1"
    assert getattr(refused.value, "__notes__", []) == [f"The step file is {tmp_path / 'steps' / 'step-b.md'}."]
    assert len(store.payloads) == written, "nothing is written"


async def test_a_step_whose_only_change_writes_a_field_at_its_default_imports_as_unchanged(tmp_path: Path) -> None:
    store, library = await _holding(STEP_A)
    written = len(store.payloads)
    _write(tmp_path, steps={"step-a.md": STEP_A_WITH_TOOLS}, workflows={})
    _write_cases(tmp_path, "step-a.yaml", CASES)

    imported = await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported.written == ("st.step_a.v1",)

    assert len(store.payloads) == written, "the same version with the same content writes nothing"


async def test_a_step_the_library_would_read_back_different_is_refused_by_its_field(tmp_path: Path) -> None:
    step = load_step_document(STEP_B_WITH_NULL_DEFAULT).step
    stored = TypeAdapter(InlineStep).validate_python(pruned(step.model_dump(mode="json")))
    assert step_content_address(stored) != step_content_address(step), "engram's ingest would store this step without its null default"
    store, library = await _holding()
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B_WITH_NULL_DEFAULT}, workflows={})

    with pytest.raises(DocumentError, match=r"step-b\.md.*inputs\.prefix\.json_schema\.default"):
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert store.payloads == []


async def test_a_step_s_case_file_is_carried_to_the_cases_of_its_page(tmp_path: Path) -> None:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    _write(tmp_path, steps={"step-a.md": STEP_A, "step-b.md": STEP_B}, workflows={})
    _write_cases(tmp_path, "step-a.yaml", CASES)

    await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert json.loads(store.written("st.step_a.v1")["cases"]) == CASES
    assert await library.fetch_step_cases("st.step_a.v1") == CASES
    assert "cases" not in store.written("st.step_b.v1"), "a step with no case file carries none"


async def test_a_step_with_no_case_file_in_the_folder_s_evals_removes_the_cases_its_version_held(tmp_path: Path) -> None:
    _, library = await _holding(STEP_A)
    assert await library.fetch_step_cases("st.step_a.v1") == CASES
    _write(tmp_path, steps={"step-a.md": STEP_A}, workflows={})
    _write_cases(tmp_path, "another-step.yaml", CASES)

    await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert await library.fetch_step_cases("st.step_a.v1") is None


async def test_a_folder_with_no_evals_folder_keeps_the_cases_each_version_holds(tmp_path: Path) -> None:
    """A partial import, such as one workflow copied with its step files alone, never strips a step's cases."""
    store, library = await _holding(STEP_A)
    written = len(store.payloads)
    _write(tmp_path, steps={"step-a.md": STEP_A}, workflows={})

    await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert await library.fetch_step_cases("st.step_a.v1") == CASES
    assert len(store.payloads) == written, "an unchanged step whose cases are kept writes nothing"


@pytest.mark.parametrize(
    "cases",
    [
        {"cases": [{"name": "no inputs"}]},
        {"cases": [{"name": "says a", "inputs": {}}], "evaluators": ["NothingRegistersThis"]},
        {"cases": [{"name": "twice", "inputs": {}}, {"name": "twice", "inputs": {}}]},
        ["not", "a", "mapping"],
    ],
    ids=["a case without inputs", "an evaluator nothing registers", "two cases sharing a name", "not a mapping"],
)
async def test_a_case_file_its_case_run_would_refuse_refuses_the_folder_by_name(tmp_path: Path, cases: object) -> None:
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    _write(tmp_path, steps={"step-a.md": STEP_A}, workflows={})
    _write_cases(tmp_path, "step-a.yaml", cases)

    with pytest.raises(DocumentError, match=r"evals/step-a\.yaml"):
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert store.payloads == []


def test_every_shipped_case_file_passes_the_check_the_import_runs() -> None:
    checked: list[str] = []
    for folder in (REPO / "harness" / "workflows", *sorted(REPO.glob("templates/*/workflows"))):
        documents = [load_step_document(path.read_text(encoding="utf-8")) for path in (folder / "steps").glob("*.md")]
        steps = {document.step.name.replace("_", "-"): document for document in documents}
        for cases_file in sorted((folder / "evals").glob("*.yaml")):
            assert cases_file.stem in steps, f"{cases_file} names no step in {folder / 'steps'}, so the import never reads it"
            document = steps[cases_file.stem]
            step_id = step_page_id(document.step.name, document.version)
            check_step_cases(step_id, yaml.safe_load(cases_file.read_text(encoding="utf-8")))
            checked.append(step_id)
    assert checked, "no shipped case file was found, so this test would pass vacuously"


async def test_the_repo_docs_steps_import_with_the_cases_their_case_file_holds(tmp_path: Path) -> None:
    source = REPO / "templates" / "repo-docs" / "workflows"
    shutil.copytree(source / "steps", tmp_path / "steps")
    shutil.copytree(source / "evals", tmp_path / "evals")
    (tmp_path / "workflows").mkdir()
    store = FakeCollection(collection="test-knowledge-base", prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    check_adrs = load_step_document((source / "steps" / "check-adrs.md").read_text(encoding="utf-8"))

    await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    shipped = yaml.safe_load((source / "evals" / "check-adrs.yaml").read_text(encoding="utf-8"))
    assert await library.fetch_step_cases(step_page_id("check_adrs", check_adrs.version)) == shipped


STEP_A_V2 = STEP_A.replace("version: 1", "version: 2").replace('"a"', '"a2"')

STEP_A_V3 = STEP_A.replace("version: 1", "version: 3").replace('"a"', '"a3"')

SAY_X_BODY = "Use when a run needs an x."

STEPS_A = {document.version: document.step for document in map(load_step_document, (STEP_A, STEP_A_V2, STEP_A_V3))}

ADOPTION = Lineage(parent_name="say-x", parent_version=3, ops=({"op": "replace_inline_step", "name": "step_a"},), touched=("a",))


def _say_x(version: int, step: InlineStep) -> Workflow:
    """`say-x` at `version`, holding a copy of `step` and running it once."""
    return Workflow(name="say-x", version=version, description="Says x.", inline=(step,), nodes=(RunNode(id="a", work=step.name),), output=FromNode(node="a"))


def _say_x_file(
    workflow: Workflow, *, title: str = "Say x", description: str = "Says x.", body: str = SAY_X_BODY, presentation: WorkflowPresentation | None = None
) -> str:
    """The workflow file of `workflow` under these words."""
    document: dict[str, object] = {"title": title, "description": description, "workflow": workflow.model_dump(mode="json")}
    if presentation is not None:
        document["presentation"] = presentation.model_dump(mode="json")
    return f"---\n{yaml.safe_dump(document, sort_keys=False)}---\n\n{body}\n"


def _stamp(workflow: Workflow, environment: Mapping[str, str] | None = None) -> WorkflowFingerprint:
    """The stamp an import over `_registry()` with `environment` writes for `workflow`."""
    return fingerprint(workflow, _registry(), environment=dict(environment or {}))


async def _moved_past(saved_by: str) -> tuple[FakeCollection, LibraryCollection]:
    """A library holding `step_a` v1 to v3, `say-x` v3 with `step_a` v1, and `say-x` v4 adopting `step_a` v2, saved by `saved_by`.

    v4 carries a lineage, a live trial marker, the stamp an import with no environment writes and a decline of `step_a`
    v3, as an adoption does, and v3 is superseded.
    """
    store, library = await _holding(STEP_A, STEP_A_V2, STEP_A_V3)
    await library.put(_say_x(3, STEPS_A[1]), title="Say x", summary="Says x.", applicability=SAY_X_BODY, provenance={"saved_by": "owner:cli"})
    await library.put(
        _say_x(4, STEPS_A[2]),
        title="Say x",
        summary="Says x.",
        applicability=SAY_X_BODY,
        provenance={"saved_by": saved_by},
        fingerprint=_stamp(_say_x(4, STEPS_A[2])),
        lineage=ADOPTION,
        live_trial=LiveTrialMarker(previous="wf.say-x.v3", step="step_a", step_hash=step_content_address(STEPS_A[2])),
    )
    await library.record_decline("wf.say-x.v4", step="step_a", step_hash=step_content_address(STEPS_A[3]), because="kept")
    assert await library.current("wf.say-x.v3") == "wf.say-x.v4"
    return store, library


async def test_a_file_the_library_moved_past_by_step_maintenance_is_skipped_by_name_and_the_rest_is_written(tmp_path: Path) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(3, STEPS_A[1])), "two-step.md": WORKFLOW})

    imported = await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported == FolderImport(
        written=("st.step_b.v1", "wf.two-step.v1"), skipped=(SkippedWorkflowFile(file=tmp_path / "workflows" / "say-x.md", version=3, reason="moved_past", newer_version=4),)
    )
    assert [payload["frontmatter"]["id"] for payload in store.payloads[before:]] == ["st.step_b.v1", "wf.two-step.v1"]


async def test_a_file_the_library_moved_past_by_anyone_else_refuses_the_folder_before_its_first_write(tmp_path: Path) -> None:
    store, library = await _moved_past("human:owner")
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(3, STEPS_A[1])), "two-step.md": WORKFLOW})

    with pytest.raises(WorkflowVersionSupersededError, match=r"say-x\.md holds wf\.say-x\.v3.*wf\.say-x\.v4, saved by human:owner") as refused:
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert (refused.value.workflow_id, refused.value.newest_id) == ("wf.say-x.v3", "wf.say-x.v4")
    assert len(store.payloads) == before, "nothing is written, not even the folder's step"


async def test_a_file_whose_workflow_and_words_equal_the_stored_version_writes_nothing_and_the_page_keeps_its_adoption(tmp_path: Path) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)
    _write(tmp_path, steps={}, workflows={"say-x.md": _say_x_file(_say_x(4, STEPS_A[2]))})

    imported = await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported == FolderImport(written=("wf.say-x.v4",), skipped=())
    assert len(store.payloads) == before, "the unchanged version writes nothing"
    assert await library.fetch_lineage("wf.say-x.v4") == ADOPTION
    assert await library.fetch_live_trial("wf.say-x.v4") == LiveTrialMarker(previous="wf.say-x.v3", step="step_a", step_hash=step_content_address(STEPS_A[2]))
    (entry,) = await library.fetch_consumer_record("wf.say-x.v4")
    assert (entry.declined, entry.declined_because) == (step_content_address(STEPS_A[3]), "kept")


@pytest.mark.parametrize(
    ("words", "named"),
    [
        ({"title": "Say x louder"}, "title"),
        ({"description": "Says x twice."}, "description"),
        ({"body": "Use when a run needs two."}, "body"),
        ({"presentation": WorkflowPresentation(purpose="Say x.")}, "presentation"),
    ],
    ids=["title", "description", "body", "presentation"],
)
async def test_a_file_whose_workflow_equals_the_stored_version_and_whose_words_differ_refuses_the_folder(
    tmp_path: Path, words: Mapping[str, Any], named: str
) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(4, STEPS_A[2]), **words)})

    with pytest.raises(WorkflowCardChangedError, match=rf"say-x\.md holds wf\.say-x\.v4 .* different {named}\. .*Raise `version`") as refused:
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert refused.value.fields == (named,)
    assert len(store.payloads) == before, "nothing is written, not even the folder's step"


async def _assert_adoption_kept(library: LibraryCollection) -> None:
    """`say-x` v4 still carries the lineage, the live trial marker and the decline `_moved_past` gave it."""
    assert await library.fetch_lineage("wf.say-x.v4") == ADOPTION
    assert await library.fetch_live_trial("wf.say-x.v4") == LiveTrialMarker(previous="wf.say-x.v3", step="step_a", step_hash=step_content_address(STEPS_A[2]))
    (entry,) = await library.fetch_consumer_record("wf.say-x.v4")
    assert (entry.declined, entry.declined_because) == (step_content_address(STEPS_A[3]), "kept")


async def test_an_unchanged_file_whose_stored_stamp_differs_gets_only_the_new_stamp_and_keeps_its_adoption(tmp_path: Path) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)
    _write(tmp_path, steps={}, workflows={"say-x.md": _say_x_file(_say_x(4, STEPS_A[2]))})
    environment = {"model": "new-model"}

    imported = await import_path(
        tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT, environment=environment
    )

    assert imported == FolderImport(written=("wf.say-x.v4",), skipped=())
    assert [payload["frontmatter"]["id"] for payload in store.payloads[before:]] == ["wf.say-x.v4"]
    assert await library.fetch_fingerprint("wf.say-x.v4") == _stamp(_say_x(4, STEPS_A[2]), environment)
    assert await library.saved_by("wf.say-x.v4") == STEP_MAINTENANCE_AGENT, "the import's own name is not written over it"
    await _assert_adoption_kept(library)


async def test_a_file_whose_version_the_library_holds_with_other_content_refuses_the_folder_before_its_first_write(tmp_path: Path) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(4, STEPS_A[3]))})

    with pytest.raises(WorkflowVersionChangedError, match=r"say-x\.md holds wf\.say-x\.v4.*other content.*Raise `version`") as refused:
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert refused.value.workflow_id == "wf.say-x.v4"
    assert len(store.payloads) == before, "nothing is written, not even the folder's step"
    await _assert_adoption_kept(library)


NULL_DEFAULT = Workflow(
    name="say-x",
    version=1,
    description="Says x.",
    parameters={"note": ParameterSpec(description="A note nobody reads.", json_schema={"type": "string", "default": None})},
    inline=(STEPS_A[1],),
    nodes=(RunNode(id="a", work="step_a"),),
    output=FromNode(node="a"),
)


async def test_a_file_the_library_stores_without_its_null_values_imports_again_as_unchanged(tmp_path: Path) -> None:
    store, library = await _holding(STEP_A)
    _write(tmp_path, steps={}, workflows={"say-x.md": _say_x_file(NULL_DEFAULT)})
    await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)
    before = len(store.payloads)

    imported = await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported.written == ("wf.say-x.v1",)
    assert len(store.payloads) == before, "the stored spec lost only the null the store drops, so the version is unchanged"


async def test_a_superseded_file_whose_newer_versions_are_all_archived_refuses_the_folder_before_its_first_write(tmp_path: Path) -> None:
    store, library = await _moved_past("human:owner")
    await library.set_archived("wf.say-x.v4", archived=True)
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(3, STEPS_A[1]))})

    with pytest.raises(WorkflowNotLiveError, match=r"say-x\.md holds wf\.say-x\.v3.*no newer version of it is live.*above 4") as refused:
        await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert refused.value.workflow_id == "wf.say-x.v3"
    assert len(store.payloads) == before, "nothing is written, not even the folder's step"


async def test_a_file_whose_version_the_library_archived_is_skipped_with_that_reason_and_stays_archived(tmp_path: Path) -> None:
    store, library = await _moved_past(STEP_MAINTENANCE_AGENT)
    assert await library.revert("wf.say-x.v4") == "wf.say-x.v3"
    before = len(store.payloads)
    _write(tmp_path, steps={"step-b.md": STEP_B}, workflows={"say-x.md": _say_x_file(_say_x(4, STEPS_A[2])), "two-step.md": WORKFLOW})

    imported = await import_path(tmp_path, library=library, registry=_registry(), by="test:save", check_cases=check_step_cases, moved_past_by=STEP_MAINTENANCE_AGENT)

    assert imported == FolderImport(
        written=("st.step_b.v1", "wf.two-step.v1"),
        skipped=(SkippedWorkflowFile(file=tmp_path / "workflows" / "say-x.md", version=4, reason="archived", newer_version=None),),
    )
    assert [payload["frontmatter"]["id"] for payload in store.payloads[before:]] == ["st.step_b.v1", "wf.two-step.v1"]
    card = await library.fetch_card("wf.say-x.v4")
    assert card is not None
    assert card.archived_at is not None
