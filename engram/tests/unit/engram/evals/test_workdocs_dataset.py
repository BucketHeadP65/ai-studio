"""Drift guards for the workdocs cold-start golden dataset (`evals/datasets/workdocs_coldstart.yaml`).

The dataset is hand-authored against the working-docs memory knowledge base (`docs/memory`), so nothing at
runtime re-derives its ground truth: these tests are the guard. They pin four ways it can rot.

* **It must still load.** `load_dataset` is the only supported reader
  (it re-registers the custom `RetrievalMetrics` evaluator so the YAML's
  evaluator spec resolves back to the live class), so a shape change in the authoring YAML or in the
  slice's value objects surfaces here rather than mid-run.
* **No case may cite an id the knowledge base never had.** Every `cited_source_ids` value must appear in
  `_CITED_IDS`. This catches an authoring slip — a typo, a guessed id, a copied id from the research
  knowledge base — and nothing else: on its own it says only that the cites are drawn from a fixed list.
* **Every id a case depends on must still be on disk.** `_CITED_IDS` is checked for *containment*
  against the frontmatter ids actually present under `docs/memory/Units/` and `docs/brainstorming/`,
  which catches a rename or a retirement that would otherwise only show up as 0 recall at run time.
  The list stays hardcoded (rather than derived) so the failure names the drift. It holds what the
  cases cite and anchor on, never the whole knowledge base: a new document no case depends on is authoring.
* **It must stay a real suite.** The cold-start acceptance run needs enough cases to average over, so
  the case count has a floor and every case carries the ground truth the evaluator scores against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.evals.generation.contracts import load_dataset
from engram.evals.harness.contracts import RetrievalMetrics
from engram.evals.shared.domain import QaCaseMeta, QaInput, RetrievalAnswer

# `engram/` — four levels up from `engram/tests/unit/evals/test_workdocs_dataset.py`; the repo root is
# one further (the knowledge base lives at `<repo>/docs/memory`, its lifecycle docs at `<repo>/docs/brainstorming`
# — the `../brainstorming` folder the workdocs knowledge_base_schema declares for the `bs.` page type).
_DATASET_PATH = Path(__file__).resolve().parents[4] / "evals" / "datasets" / "workdocs_coldstart.yaml"
_REPO_ROOT = Path(__file__).resolve().parents[5]
_UNITS_DIR = _REPO_ROOT / "docs" / "memory" / "Units"
_LIFECYCLE_DIR = _REPO_ROOT / "docs" / "brainstorming"

# The floor the cold-start acceptance run averages over.
_MIN_CASES = 6

# Every page id the dataset's cases cite or anchor on. Hardcoded on purpose, so that a rename or a
# retirement of a page a case depends on fails here and names itself, rather than scoring 0 recall at
# run time. It is not the knowledge base: documents land in these folders by authoring, and a new one no case
# cites is not drift.
_CITED_IDS = frozenset(
    {
        "mu.disclosure-envelope",
        "mu.emission-contract",
        "mu.enforcement-tiers",
        "mu.evidence-kinds-doctrine",
        "mu.lifecycle-as-consolidation",
        "mu.three-planes-model",
        "mu.verdict-binding-algebra",
        "hub.judges-thread",
        "hub.memory-thread",
        "bs.judges-operators-telemetry",
        "bs.memory-engineering-lifecycle",
    }
)


_FRONTMATTER_FENCE = "---"
_ID_KEY = "id:"


def _frontmatter_id(path: Path) -> str | None:
    """The `id` a markdown file declares in its YAML frontmatter, or None when it declares none.

    A deliberately tiny reader rather than a YAML parse: the only field this guard needs is `id`, and
    keeping it stdlib-only means the drift check has no dependency the knowledge base itself does not have.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return None
    for line in lines[1:]:
        if line.strip() == _FRONTMATTER_FENCE:
            return None
        if line.startswith(_ID_KEY):
            return line.removeprefix(_ID_KEY).strip().strip("\"'")
    return None


def _knowledge_base_ids_on_disk() -> frozenset[str]:
    """Every page id the working-docs knowledge base actually carries right now.

    The units and hubs are every `docs/memory/Units/*.md`; the lifecycle documents are the `bs.`-prefixed
    ids under `docs/brainstorming/` (the folder the knowledge_base_schema's `brainstorming-doc` page type points at),
    filtered by prefix so an unrelated file landing there cannot silently widen the set.
    """
    unit_ids = {found for path in _UNITS_DIR.glob("*.md") if (found := _frontmatter_id(path)) is not None}
    lifecycle_ids = {
        found for path in _LIFECYCLE_DIR.glob("*.md") if (found := _frontmatter_id(path)) is not None and found.startswith("bs.")
    }
    return frozenset(unit_ids | lifecycle_ids)


@pytest.fixture(scope="module")
def dataset():
    """The loaded cold-start dataset, shared across the module's assertions."""
    return load_dataset(_DATASET_PATH)


def test_dataset_loads_with_the_retrieval_evaluator(dataset) -> None:
    assert dataset.name == "engram-retrieval-golden"
    assert any(isinstance(evaluator, RetrievalMetrics) for evaluator in dataset.evaluators)


def test_dataset_carries_enough_cases_to_average_over(dataset) -> None:
    assert len(dataset.cases) >= _MIN_CASES


def test_every_case_is_fully_typed_and_named(dataset) -> None:
    names = [case.name for case in dataset.cases]
    assert len(set(names)) == len(names)
    for case in dataset.cases:
        assert isinstance(case.inputs, QaInput)
        assert isinstance(case.metadata, QaCaseMeta)
        assert isinstance(case.expected_output, RetrievalAnswer)
        assert case.inputs.question.strip()
        assert case.expected_output.answer.strip()


def test_every_id_a_case_depends_on_is_still_on_disk() -> None:
    # The half that actually catches drift: a page a case cites gets renamed or retired, and the case
    # goes on citing an id that no longer resolves, scoring 0 recall at run time with nothing failing
    # here. Containment, not equality: a new document nobody cites is authoring, not drift.
    on_disk = _knowledge_base_ids_on_disk()
    assert on_disk, f"found no knowledge base ids under {_UNITS_DIR} / {_LIFECYCLE_DIR}, so the drift guard would pass vacuously"
    assert on_disk >= _CITED_IDS, f"ids this dataset depends on are gone from disk: {sorted(_CITED_IDS - on_disk)}"


def test_every_cited_id_names_a_real_knowledge_base_page(dataset) -> None:
    cited = {source_id for case in dataset.cases for source_id in case.expected_output.cited_source_ids}
    on_disk = _knowledge_base_ids_on_disk()
    assert cited, "the dataset scores recall over cited ids, so at least one case must carry gold ids"
    assert cited <= on_disk, f"cases cite ids absent from the workdocs knowledge_base: {sorted(cited - on_disk)}"
    assert cited <= _CITED_IDS, f"cases cite ids this test's own pin does not list: {sorted(cited - _CITED_IDS)}"


def test_every_case_anchors_on_a_real_knowledge_base_page(dataset) -> None:
    # `anchor_page_id` is the case's provenance (which page it was authored against), scored by nothing but
    # read by the coverage/top-up tooling — a stale one is the same rename drift as a stale gold id.
    anchors = {case.metadata.anchor_page_id for case in dataset.cases}
    on_disk = _knowledge_base_ids_on_disk()
    assert anchors <= on_disk, f"cases anchor on ids absent from the workdocs knowledge_base: {sorted(anchors - on_disk)}"
    assert anchors <= _CITED_IDS, f"cases anchor on ids this test's own pin does not list: {sorted(anchors - _CITED_IDS)}"


def test_cases_span_both_threads_and_all_three_difficulty_bands(dataset) -> None:
    cited = {source_id for case in dataset.cases for source_id in case.expected_output.cited_source_ids}
    assert "hub.judges-thread" in cited
    assert "hub.memory-thread" in cited
    assert {case.metadata.difficulty for case in dataset.cases} == {"easy", "medium", "hard"}
