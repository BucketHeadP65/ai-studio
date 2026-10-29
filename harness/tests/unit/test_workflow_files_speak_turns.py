"""Every shipped block, walked cold, and every template workflow read the way an import reads it.

No map node may still carry a retired chain-syntax field, and every presentation a file declares is
one the models recognise. The templates are then held to two further bars: each one is described
well enough to keep, and importing the folder writes exactly the work its own files bring.

Parsed with plain YAML rather than the grammar's own loader -- the point is to catch a block the
grammar would refuse outright, not to lean on the refusal. The import half does use the loader,
because that is what the importer itself does.
"""

from __future__ import annotations

import shutil

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from core.ai.workflows.contracts import (
    JudgeGate,
    LibraryCollection,
    MapNode,
    StepRegistry,
    WorkflowPresentation,
    load_spec_document,
    load_step_document,
    prepare,
    referenced_steps,
    step_page_id,
)
from core.ai.workflows.domain.vocabulary import SameAs
from pydantic import TypeAdapter

from harness.domain.ports import JUDGE_TOOLS
from harness.domain.readiness import card_readiness
from harness.features.browsing.importing import import_path
from tests.support.grants import app_grants

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.workflows.contracts import Briefing, CodeRunner, GateDecision, InlineStep, Program, Workflow

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"
TEMPLATES = Path(__file__).resolve().parents[3] / "templates"

TEMPLATE_WORKFLOW_DIRS = (
    TEMPLATES / "research" / "workflows",
    TEMPLATES / "repo-docs" / "workflows",
    TEMPLATES / "benchmark-dataset" / "workflows",
    TEMPLATES / "news-benchmark" / "workflows",
)
"""The four template folders `harness import` is pointed at, each holding its own `steps/` and `workflows/`."""

IMPORTED_SUBDIRS = ("steps", "workflows")
"""The only two subfolders an import reads. Anything else a template folder keeps is not a block."""

HOST_WORK = frozenset({"notify_owner"})
"""Work this app registers itself, so no file has to bring it. Everything else a graph names is brought."""

_OLD_MAP_FIELDS = ("work", "then", "then_each", "then_inputs")

_USES = TypeAdapter(list[str])
"""Validates a workflow file's `uses` key the same way the importer validates it."""


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


def _map_nodes(document: dict[str, object]) -> list[dict[str, object]]:
    workflow = document.get("workflow")
    if not isinstance(workflow, dict):
        return []
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict) and node.get("kind") == "map"]


def test_no_shipped_map_node_carries_a_retired_chain_field() -> None:
    for path in sorted(WORKFLOW_FILES.glob("**/*.md")):
        document = _frontmatter(path)
        for node in _map_nodes(document):
            for field in _OLD_MAP_FIELDS:
                assert field not in node, f"{path.relative_to(WORKFLOW_FILES)} node {node.get('id')!r} still carries {field!r}"


def test_every_shipped_map_node_names_a_non_empty_turn_list() -> None:
    seen_a_map = False
    for path in sorted(WORKFLOW_FILES.glob("**/*.md")):
        document = _frontmatter(path)
        for node in _map_nodes(document):
            seen_a_map = True
            turns = node.get("turns")
            assert isinstance(turns, list), f"{path.relative_to(WORKFLOW_FILES)} node {node.get('id')!r} names no turns list"
            assert turns, f"{path.relative_to(WORKFLOW_FILES)} node {node.get('id')!r} has an empty turn list"
    assert seen_a_map, "expected at least one shipped map node to exercise this test"


def test_every_shipped_presentation_is_one_the_models_recognise() -> None:
    """A file declaring presentation frontmatter the importer cannot parse would refuse the folder.

    The walk itself is asserted live: a glob that stopped finding files would otherwise make this
    pass by finding nothing to check.
    """
    walked = 0
    for directory in (WORKFLOW_FILES, *TEMPLATE_WORKFLOW_DIRS):
        for subdir in IMPORTED_SUBDIRS:
            for path in sorted((directory / subdir).glob("*.md")):
                walked += 1
                declared = _frontmatter(path).get("presentation")
                if declared is not None:
                    WorkflowPresentation.model_validate(declared)
    assert walked, "no shipped workflow file was read"


# --- the templates, as the importer reads them ----------------------------------------------


async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Stands in for a performer: preparing a spec only checks that this run could perform a briefing."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _run_code(step: Program, output_type: type, /, **inputs: object) -> object:
    """Stands in for a sandbox: preparing a spec only checks that this run has one."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _decide_gate(gate_id: str, charge: str, value: object, /) -> GateDecision:
    """Stands in for an orchestrator: preparing a spec only checks that this run has one."""
    msg = "nothing should run"
    raise AssertionError(msg)


async def _registry(run_code: CodeRunner) -> StepRegistry:
    """A registry offering exactly what this app offers, plus the one host step the template workflows name."""
    grantable = await app_grants()
    registry = StepRegistry(
        perform=_perform,
        grants=grantable,
        run_code=run_code,
        decide_gate=_decide_gate,
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )

    async def notify_owner(collection: str, written: int, needs_you: int = 0) -> str:
        return f"{written} in {collection}"

    registry.register(notify_owner, name="notify_owner", replay_safe=False)
    return registry


class _FakeRows:
    """The two asyncpg calls the library's read side makes, answered from an empty script."""

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        del sql, args
        return ()

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        del sql, args
        return None


class _FakeIngest:
    """Records every payload an import hands the store, in the order it hands them over."""

    def __init__(self) -> None:
        self.payloads: list[tuple[Mapping[str, Any], str]] = []

    async def __call__(self, payload: Mapping[str, Any], *, collection: str) -> None:
        self.payloads.append((payload, collection))


def _importable(tmp_path: Path, source: Path) -> Path:
    """One template folder copied where the reusable steps this app ships sit beside it.

    A real import loads `harness/workflows` first, so `st.finish.v2` is already in the library when a
    template naming it arrives. Nothing here holds a library between two imports, so this app's own
    step files are copied in instead and the importer finds them in the folder.
    """
    folder = tmp_path / source.parent.name
    (folder / "steps").mkdir(parents=True)
    (folder / "workflows").mkdir(parents=True)
    for step in sorted((WORKFLOW_FILES / "steps").glob("*.md")) + sorted((source / "steps").glob("*.md")):
        shutil.copy(step, folder / "steps" / step.name)
    for workflow in sorted((source / "workflows").glob("*.md")):
        shutil.copy(workflow, folder / "workflows" / workflow.name)
    return folder


def _steps_of(folder: Path) -> dict[str, InlineStep]:
    """Every step file in the folder, under the id the library mints for it: its name and its declared version."""
    docs = [load_step_document(path.read_text(encoding="utf-8")) for path in sorted((folder / "steps").glob("*.md"))]
    return {step_page_id(doc.step.name, doc.version): doc.step for doc in docs}


def _assembled(path: Path, steps: dict[str, InlineStep]) -> tuple[Workflow, frozenset[str]]:
    """One workflow file with its `uses` steps copied in, and the work names the file itself brings.

    Each `uses` entry is resolved by whole id, the way the importer fetches one out of the library, so a
    file naming a version no step file declares fails here rather than silently taking another version.
    """
    document = load_spec_document(path.read_text(encoding="utf-8"))
    uses = tuple(_USES.validate_python(_frontmatter(path).get("uses", [])))
    missing = [step_id for step_id in uses if step_id not in steps]
    assert not missing, f"{path.name} uses {', '.join(missing)}, which no step file beside it declares. On file: {sorted(steps)}"
    copied = tuple(steps[step_id] for step_id in uses)
    brought = frozenset(step.name for step in copied) | frozenset(task.name for task in document.workflow.inline)
    return document.workflow.model_copy(update={"inline": (*document.workflow.inline, *copied)}), brought


def _template_workflows(tmp_path: Path) -> list[tuple[str, Path, dict[str, InlineStep]]]:
    found: list[tuple[str, Path, dict[str, InlineStep]]] = []
    for source in TEMPLATE_WORKFLOW_DIRS:
        folder = _importable(tmp_path, source)
        steps = _steps_of(folder)
        found.extend((f"{source.parent.name}/{path.stem}", path, steps) for path in sorted((folder / "workflows").glob("*.md")))
    return found


async def test_every_template_workflow_is_described_well_enough_to_keep(tmp_path: Path) -> None:
    """The card a person chooses by, held to the same bar a draft is held to before it is saved."""
    registry = await _registry(_run_code)
    checked = 0
    for name, path, steps in _template_workflows(tmp_path):
        checked += 1
        spec, _ = _assembled(path, steps)
        document = _frontmatter(path)
        declared = document.get("presentation")
        readiness = card_readiness(
            spec,
            prepare(spec, registry),
            title=str(document.get("title") or spec.name),
            summary=str(document.get("description") or spec.description),
            presentation=None if declared is None else WorkflowPresentation.model_validate(declared),
        )
        assert readiness.described, f"{name}: " + "; ".join(f"{f.field}: {f.message}" for f in readiness.errors())
        assert readiness.executable, f"{name} does not execute on this app's own steps"
    assert checked == 10, f"expected the ten shipped template workflows, walked {checked}"


async def test_a_summary_repeating_the_spec_description_is_advice_and_not_a_refusal(tmp_path: Path) -> None:
    """An author may legitimately choose the same line twice, so the warning must never refuse the write."""
    registry = await _registry(_run_code)
    _, path, steps = _template_workflows(tmp_path)[0]
    spec, _ = _assembled(path, steps)
    document = _frontmatter(path)
    declared = document.get("presentation")

    readiness = card_readiness(
        spec,
        prepare(spec, registry),
        title=str(document.get("title")),
        summary=f"  {spec.description.upper()}  ",
        presentation=None if declared is None else WorkflowPresentation.model_validate(declared),
    )

    assert readiness.described, "a repeated summary must not refuse a workflow that is otherwise described"
    assert [finding.severity for finding in readiness.findings if finding.field == "summary"] == ["warning"]


def _named_by_likes(spec: Workflow, start: Collection[str]) -> set[str]:
    """Every step a `like` reaches from the steps in `start`, following each reached step's own `like`s in turn."""
    by_name = {step.name: step for step in spec.inline}
    reached: set[str] = set()
    pending = list(start)
    while pending:
        step = by_name.get(pending.pop())
        if step is None:
            continue
        for slot in (*step.inputs.values(), step.output):
            if isinstance(slot, SameAs) and slot.like not in reached:
                reached.add(slot.like)
                pending.append(slot.like)
    return reached


async def test_importing_a_template_brings_exactly_the_work_its_own_files_name(tmp_path: Path) -> None:
    """Every work a graph runs is either brought by the file's `uses` or registered by this app.

    Both directions are asserted: no graph names work nothing supplies, and no `uses` entry rides
    along unreferenced. A `uses` entry a run step's `like` chain names is referenced: it brings the
    type that step takes.
    """
    for name, path, steps in _template_workflows(tmp_path):
        spec, brought = _assembled(path, steps)
        assert _brings_exactly_its_work(spec, brought), name


def _brings_exactly_its_work(spec: Workflow, brought: Collection[str]) -> bool:
    """Whether every step a node runs is brought, and every step brought is run or named by a run step's `like` chain."""
    run = set(referenced_steps(spec)) - HOST_WORK
    return run <= set(brought) and set(brought) <= run | _named_by_likes(spec, run)


async def test_a_uses_entry_only_a_like_chain_names_is_referenced_and_one_nothing_names_is_refused(tmp_path: Path) -> None:
    """The type-only steps a lone link turn needs count as referenced, and a step no node or `like` reaches is refused."""
    name, path, steps = next(found for found in _template_workflows(tmp_path) if found[0] == "news-benchmark/news-link")
    spec, brought = _assembled(path, steps)
    run = set(referenced_steps(spec)) - HOST_WORK
    assert {"digest_source", "find_undigested"} <= set(brought) - run, name
    assert _brings_exactly_its_work(spec, brought), name

    uses = "uses: [st.find_undigested.v4, st.digest_source.v11, st.link_entities.v2]"
    text = path.read_text(encoding="utf-8")
    assert uses in text
    extra = path.with_name("news-link-with-an-extra-step.md")
    extra.write_text(text.replace(uses, uses.removesuffix("]") + ", st.relate_digest.v2]"), encoding="utf-8")
    spec, brought = _assembled(extra, steps)

    assert "relate_digest" in brought
    assert not _brings_exactly_its_work(spec, brought), "a uses entry no node runs and no like names rides along"


async def test_every_map_turn_and_judge_of_a_template_builds_on_this_app_s_registry(tmp_path: Path) -> None:
    """A map's turns and a judge's work are the steps most likely to fall off the registry, so both are named."""
    registry = await _registry(_run_code)
    fitted: set[str] = set()
    for name, path, steps in _template_workflows(tmp_path):
        spec, _ = _assembled(path, steps)
        ready = prepare(spec, registry)
        assert ready.report.ok, (name, ready.report.issues)
        wanted = [turn for node in spec.nodes if isinstance(node, MapNode) for turn in node.turns]
        wanted += [node.work for node in spec.nodes if isinstance(node, JudgeGate)]
        assert wanted, f"{name} names no map turn and no judge, so this assertion would pass vacuously"
        for work in wanted:
            assert ready.registry.get(work) is not None, f"{name} names {work!r}, which this app cannot build"
            fitted.add(work)
    assert "check_adrs" in fitted, "the judge step must be among the work this app fits"


async def test_importing_every_template_folder_writes_every_step_then_every_workflow(tmp_path: Path) -> None:
    """The folders, imported into a library of their own, with nothing reaching a real store."""
    registry = await _registry(_run_code)
    written: list[str] = []
    for source in TEMPLATE_WORKFLOW_DIRS:
        folder = _importable(tmp_path, source)
        ingest = _FakeIngest()
        store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

        ids = await import_path(folder, library=store, registry=registry, by="test:import")

        types = [str(payload["frontmatter"]["type"]) for payload, _ in ingest.payloads]
        assert types == sorted(types, key=lambda kind: kind != "step"), f"{source.name} wrote a workflow before a step it uses"
        written.extend(ids)
    assert [page_id for page_id in written if page_id.startswith("wf.")] == [
        "wf.research-compound.v12",
        "wf.research-decision-brief.v1",
        "wf.research-digest.v16",
        "wf.research-stance-refresh.v1",
        "wf.docs-notes-to-analysis.v2",
        "wf.docs-promote-initiative.v6",
        "wf.benchmark-digest.v3",
        "wf.news-digest.v2",
        "wf.news-link.v1",
        "wf.news-relate.v2",
    ]


async def test_an_imported_template_workflow_carries_the_words_its_file_declared(tmp_path: Path) -> None:
    """The template's own sentences travel into the library, and nothing is inferred from the spec."""
    folder = _importable(tmp_path, TEMPLATES / "research" / "workflows")
    ingest = _FakeIngest()
    store = LibraryCollection(_FakeRows(), ingest, collection="test-knowledge-base")

    await import_path(folder, library=store, registry=await _registry(_run_code), by="test:import")

    cards = {str(payload["frontmatter"]["id"]): payload["frontmatter"] for payload, _ in ingest.payloads}
    digest = cards["wf.research-digest.v16"]
    declared = _frontmatter(folder / "workflows" / "research-digest.md")["presentation"]
    assert digest["presentation"] == WorkflowPresentation.model_validate(declared).model_dump(mode="json")
    front = _frontmatter(folder / "workflows" / "research-digest.md")
    workflow = front["workflow"]
    assert isinstance(workflow, dict)
    assert digest["summary"] == front["description"], "the card's line is the file's own description key"
    assert digest["spec"]["description"] == workflow["description"], "the spec keeps the description it declares, whatever the card says"
