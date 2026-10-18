"""Drift guards for the estate cold-start golden dataset (`evals/datasets/repo_docs_estate.yaml`).

The dataset is hand-authored against the indexed repo-docs estate (`docs/knowledge-base-schema.json`), so nothing at
runtime re-derives its ground truth: these tests are the guard. Unlike the workdocs guard
(`test_workdocs_dataset.py`), the estate is large (a dozen page kinds, over a hundred pages) and grows on its
own schedule, so the on-disk id universe is *derived* every run from `load_knowledge_base_schema` rather than
hardcoded — the derivation is always fresh, so only the dataset's own cites can go stale.

* **It must still load.** `load_dataset` is the only supported reader (it re-registers the custom
  `RetrievalMetrics` evaluator so the YAML's evaluator spec resolves back to the live class), so a shape
  change in the authoring YAML or in the slice's value objects surfaces here rather than mid-run.
* **No case may cite an id the estate never had.** Every `cited_source_ids` value must appear on disk right
  now. This catches an authoring slip — a typo, a guessed id, a copied id from another knowledge base.
* **No case may anchor on a retired or renamed page.** Every `anchor_page_id` (the case's provenance) must also
  resolve on disk, the same rename-drift check `cited_source_ids` gets.
* **It must stay a real suite.** The cold-start acceptance run needs enough cases to average over, spanning
  the estate's core kinds and all three difficulty bands, so a suite that quietly narrowed to one kind or
  one difficulty is caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.evals.generation.contracts import load_dataset
from engram.evals.harness.contracts import RetrievalMetrics
from engram.evals.shared.domain import QaCaseMeta, QaInput, RetrievalAnswer
from engram.kb.contracts import load_knowledge_base_schema

# `engram/` — four levels up from `engram/tests/unit/engram/evals/test_estate_dataset.py`; the repo root is
# one further (the estate's knowledge base root is `<repo>/docs`, declaring `<repo>/docs/knowledge-base-schema.json`).
_DATASET_PATH = Path(__file__).resolve().parents[4] / "evals" / "datasets" / "repo_docs_estate.yaml"
_REPO_ROOT = Path(__file__).resolve().parents[5]
_DOCS_ROOT = _REPO_ROOT / "docs"

# The floor the cold-start acceptance run averages over.
_MIN_CASES = 6

# The kinds a cold-start suite over the estate must exercise at minimum (binding rule of the dataset, not
# of the schema — the estate's knowledge-base-schema.json declares more kinds than this).
_REQUIRED_KINDS = frozenset({"adr", "architecture", "reference", "module", "glossary"})

_FRONTMATTER_FENCE = "---"
_ID_KEY = "id:"
_TYPE_KEY = "type:"


def _frontmatter_id_and_page_type(path: Path) -> tuple[str, str] | None:
    """The `(id, type)` pair a markdown file declares in its YAML frontmatter, or None if either is absent.

    A deliberately tiny reader rather than a YAML parse, mirroring the workdocs guard's reader: the two
    fields this guard needs are `id` and `type`. The estate classifies a page's kind by its frontmatter
    `type`, not by which folder it lives under — a page can live in a folder shared with another kind while
    declaring a different `type` (e.g. `docs/adr/README.md` lives beside the ADRs but declares
    `type: explanation`), so folder location alone would misclassify it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return None
    found_id: str | None = None
    found_type: str | None = None
    for line in lines[1:]:
        if line.strip() == _FRONTMATTER_FENCE:
            break
        if line.startswith(_ID_KEY):
            found_id = line.removeprefix(_ID_KEY).strip().strip("\"'")
        elif line.startswith(_TYPE_KEY):
            found_type = line.removeprefix(_TYPE_KEY).strip().strip("\"'")
    if found_id is None or found_type is None:
        return None
    return found_id, found_type


def _estate_ids_by_page_type() -> dict[str, frozenset[str]]:
    """Every page id the indexed estate carries right now, grouped by its declared kind.

    Walks `load_knowledge_base_schema(docs/).note_folders` (globbed recursively, matching the index reader) and
    `.note_files`, knowledge_base-root-relative to `docs/` exactly as the spec declares them — the same surfaces
    `studio index` reads for this knowledge base. A folder or file the spec declares that is absent on disk (an
    app without its own `docs/adr` yet) contributes nothing rather than erroring, matching the indexer's
    own fault-tolerant globbing.
    """
    spec = load_knowledge_base_schema(_DOCS_ROOT)
    by_page_type: dict[str, set[str]] = {}

    def record(path: Path) -> None:
        found = _frontmatter_id_and_page_type(path)
        if found is None:
            return
        page_id, kind = found
        by_page_type.setdefault(kind, set()).add(page_id)

    for folder in spec.note_folders:
        base = _DOCS_ROOT / folder
        if base.is_dir():
            for path in base.glob("**/*.md"):
                record(path)
    for rel in spec.note_files:
        path = _DOCS_ROOT / rel
        if path.is_file():
            record(path)

    return {kind: frozenset(ids) for kind, ids in by_page_type.items()}


@pytest.fixture(scope="module")
def dataset():
    """The loaded cold-start dataset, shared across the module's assertions."""
    return load_dataset(_DATASET_PATH)


@pytest.fixture(scope="module")
def estate_ids_by_page_type() -> dict[str, frozenset[str]]:
    """The on-disk id universe for the indexed estate, grouped by kind, shared across assertions."""
    return _estate_ids_by_page_type()


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


def test_the_estate_carries_the_required_page_types_on_disk(estate_ids_by_page_type) -> None:
    # If the estate stopped indexing one of these kinds (an app's `docs/adr` folder disappears, a kind is
    # renamed in knowledge-base-schema.json), the on-disk id set for it goes empty and every cite against it would
    # fail below — this test names which kind, rather than that failing vacuously.
    for kind in _REQUIRED_KINDS:
        assert estate_ids_by_page_type.get(kind), f"found no on-disk ids for kind {kind!r} — the estate has drifted"


def test_every_cited_id_names_a_real_estate_page(dataset, estate_ids_by_page_type) -> None:
    all_ids: frozenset[str] = frozenset().union(*estate_ids_by_page_type.values())
    cited = {source_id for case in dataset.cases for source_id in case.expected_output.cited_source_ids}
    assert cited, "the dataset scores recall over cited ids, so at least one case must carry gold ids"
    assert cited <= all_ids, f"cases cite ids absent from the indexed estate: {sorted(cited - all_ids)}"


def test_every_case_anchors_on_a_real_estate_page(dataset, estate_ids_by_page_type) -> None:
    # `anchor_page_id` is the case's provenance (which page it was authored against), scored by nothing but read
    # by the coverage/top-up tooling — a stale one is the same rename drift as a stale gold id.
    all_ids: frozenset[str] = frozenset().union(*estate_ids_by_page_type.values())
    assert {case.metadata.anchor_page_id for case in dataset.cases} <= all_ids


def test_cases_span_the_required_page_types_and_all_three_difficulty_bands(dataset, estate_ids_by_page_type) -> None:
    id_to_page_type = {page_id: kind for kind, ids in estate_ids_by_page_type.items() for page_id in ids}
    anchored_page_types = {id_to_page_type[case.metadata.anchor_page_id] for case in dataset.cases if case.metadata.anchor_page_id in id_to_page_type}
    assert anchored_page_types >= _REQUIRED_KINDS, f"no case anchors on kind(s): {sorted(_REQUIRED_KINDS - anchored_page_types)}"
    assert {case.metadata.difficulty for case in dataset.cases} == {"easy", "medium", "hard"}
