"""Round-trip and mapping tests for the golden-dataset persistence module.

The deliverable these tests pin down is the **expected-output contract**: the authoring-side :class:`~engram.evals.shared.domain.ExpectedQa` carries
`expected_source_ids` (page ids), but every pydantic-evals `Case` stores the ground truth in
the *same* :class:`~engram.evals.shared.domain.RetrievalAnswer` shape the evaluator scores against, so
`ExpectedQa.expected_source_ids` must land on `Case.expected_output.cited_source_ids`.

The core test is the full YAML round-trip — `build_dataset → save_dataset → load_dataset` —
asserting that `inputs` / `expected_output.cited_source_ids` / `metadata` survive and, most
importantly, that the custom :class:`~engram.evals.harness.contracts.RetrievalMetrics` evaluator is
reconstructed on reload (it is stored by serialization name in the YAML and resolved via
`from_file(custom_evaluator_types=[RetrievalMetrics])`).
"""

from __future__ import annotations

import pytest

from engram.evals.generation.contracts import (
    AuthoredCase,
    DatasetStamp,
    build_dataset,
    build_logfire_dataset_client,
    dataset_version_path,
    load_dataset,
    push_dataset_to_logfire,
    read_current,
    require_slug,
    save_dataset,
    save_dataset_version,
    strip_version,
    used_case_names,
    version_chain,
    write_current,
)
from engram.evals.generation.domain.datasets import require_basename, stamp_path, to_case, version_of
from engram.evals.harness.contracts import RetrievalMetrics
from engram.evals.shared.domain import ExpectedQa, QaCaseMeta, QaDifficulty, QaInput, RetrievalAnswer


def _authored(
    *,
    question: str = "What is the reported accuracy?",
    task: str | None = None,
    answer: str = "94.2% on the held-out set.",
    source_ids: tuple[str, ...] = ("paper-a.results", "paper-a.method"),
    paper_id: str = "paper-a",
    difficulty: QaDifficulty = "easy",
) -> AuthoredCase:
    """Build an :class:`AuthoredCase` with sensible defaults for the tests."""
    return AuthoredCase(
        question=QaInput(question=question, task=task),
        expected=ExpectedQa(answer=answer, expected_source_ids=source_ids),
        meta=QaCaseMeta(anchor_page_id=paper_id, difficulty=difficulty),
    )


def test_to_case_maps_expected_source_ids_onto_cited_source_ids() -> None:
    expected = ExpectedQa(answer="Yes.", expected_source_ids=("paper-b.intro", "paper-b.fig2"))
    meta = QaCaseMeta(anchor_page_id="paper-b", difficulty="hard")

    case = to_case(QaInput(question="Does it generalize?"), expected, meta)

    assert isinstance(case.expected_output, RetrievalAnswer)
    assert case.expected_output.answer == "Yes."
    # The authoring-shape page ids land on the scored shape's cited_source_ids (the contract).
    assert case.expected_output.cited_source_ids == ["paper-b.intro", "paper-b.fig2"]


def test_to_case_threads_inputs_and_metadata_through() -> None:
    question = QaInput(question="How fast?", task="benchmark")
    meta = QaCaseMeta(anchor_page_id="paper-c", difficulty="medium")

    case = to_case(question, ExpectedQa(answer="12ms", expected_source_ids=("paper-c.bench",)), meta)

    assert case.inputs == question
    assert case.metadata == meta


def test_to_case_accepts_a_bare_string_question() -> None:
    case = to_case("Plain question?", ExpectedQa(answer="A", expected_source_ids=("p.x",)), QaCaseMeta(anchor_page_id="p", difficulty="easy"))

    assert case.inputs == QaInput(question="Plain question?")


def test_to_case_assigns_a_stable_name_from_anchor_page_id() -> None:
    meta = QaCaseMeta(anchor_page_id="paper-d", difficulty="hard")

    first = to_case("q1", ExpectedQa(answer="a", expected_source_ids=("paper-d.x",)), meta, index=0)
    second = to_case("q2", ExpectedQa(answer="b", expected_source_ids=("paper-d.y",)), meta, index=1)

    assert first.name is not None
    assert "paper-d" in first.name
    assert first.name != second.name  # the index disambiguates same-paper cases


def test_build_dataset_assigns_unique_case_names_and_attaches_the_evaluator() -> None:
    dataset = build_dataset([_authored(paper_id="paper-a"), _authored(paper_id="paper-a", question="Second?")])

    assert len(dataset.cases) == 2
    assert len({case.name for case in dataset.cases}) == 2  # no duplicate names within one paper
    assert any(isinstance(evaluator, RetrievalMetrics) for evaluator in dataset.evaluators)


def test_to_case_honors_an_authoritative_name_over_the_positional_derivation() -> None:
    meta = QaCaseMeta(anchor_page_id="paper-e", difficulty="easy")

    case = to_case("q", ExpectedQa(answer="a", expected_source_ids=("paper-e.x",)), meta, index=3, name="trace:a>b#0")

    assert case.name == "trace:a>b#0"


def test_build_dataset_honors_case_name_and_falls_back_positionally() -> None:
    named = _authored().model_copy(update={"case_name": "survey:cites:hub#1"})

    dataset = build_dataset([named, _authored(question="Second?")])

    assert dataset.cases[0].name == "survey:cites:hub#1"  # the seed-derived identity wins
    assert dataset.cases[1].name is not None
    assert "paper-a" in dataset.cases[1].name  # an unnamed record keeps the positional derivation


class TestPathHardening:
    """CLI/disk-sourced path components fail loudly before any join."""

    def test_require_slug_accepts_plain_names(self):
        assert require_slug("research", source="collection") == "research"
        assert require_slug("p6smoke.v1", source="name") == "p6smoke.v1"

    @pytest.mark.parametrize("evil", ["../evil", "a/b", "/abs", "..", ".", ""])
    def test_require_slug_rejects_separators_and_dot_directories(self, evil: str):
        with pytest.raises(ValueError, match="plain name"):
            require_slug(evil, source="collection")

    def test_require_basename_accepts_a_bare_file_name(self):
        assert require_basename("research.v2.yaml", source="pointer") == "research.v2.yaml"

    @pytest.mark.parametrize("evil", ["../evil.yaml", "/etc/passwd", "a/b.yaml", "..", ""])
    def test_require_basename_rejects_traversal(self, evil: str):
        with pytest.raises(ValueError, match="bare file name"):
            require_basename(evil, source="pointer")

    def test_read_current_rejects_a_tampered_pointer_body(self, tmp_path):
        (tmp_path / "research.current").write_text("../outside.yaml", encoding="utf-8")

        with pytest.raises(ValueError, match="bare file name"):
            read_current(tmp_path, "research")

    # The three chain-name interpolations below guard with `require_slug`, matching the frozen sidecar.

    @pytest.mark.parametrize("evil", ["../evil", "a/b", ".."])
    def test_dataset_version_path_rejects_a_non_slug_name(self, tmp_path, evil: str):
        with pytest.raises(ValueError, match="plain name"):
            dataset_version_path(tmp_path, evil, 1)

    @pytest.mark.parametrize("evil", ["../evil", "a/b", ".."])
    def test_read_current_rejects_a_non_slug_name(self, tmp_path, evil: str):
        with pytest.raises(ValueError, match="plain name"):
            read_current(tmp_path, evil)

    @pytest.mark.parametrize("evil", ["../evil", "a/b", ".."])
    def test_write_current_rejects_a_non_slug_name(self, tmp_path, evil: str):
        with pytest.raises(ValueError, match="plain name"):
            write_current(tmp_path, evil, tmp_path / "research.v1.yaml")

    def test_used_case_names_rejects_a_tampered_parent_version(self, tmp_path):
        # A stamp whose parent_version is `..` slips through `with_name` (which only rejects
        # separators); the explicit basename guard turns the tampering into a boundary error
        # instead of a silently-truncated (under-reserving) name walk.
        version = tmp_path / "research.v2.yaml"
        stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=1, parent_version="..", coverage={}, retired=())
        stamp_path(version).write_text(stamp.model_dump_json(), encoding="utf-8")

        with pytest.raises(ValueError, match="bare file name"):
            used_case_names(version, ["q1"])

    def test_version_chain_rejects_a_tampered_parent_version(self, tmp_path):
        # The `version_chain` walker shares the same basename guard as `used_case_names`: a `..`
        # parent_version is a boundary error rather than a silent traversal.
        version = tmp_path / "research.v2.yaml"
        stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=1, parent_version="../evil.yaml", coverage={}, retired=())
        stamp_path(version).write_text(stamp.model_dump_json(), encoding="utf-8")

        with pytest.raises(ValueError, match="bare file name"):
            version_chain(version)


class TestVersionChain:
    """`version_chain` walks the `parent_version` stamp links root-first."""

    def _save(self, datasets_dir, name: str, version: int, *, parent: str | None = None):
        """Persist one immutable version + stamp (a `parent_version` link) and return its path."""
        path = dataset_version_path(datasets_dir, name, version)
        stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=1, parent_version=parent, coverage={}, retired=())
        save_dataset_version(build_dataset([_authored()]), stamp, path)
        return path

    def test_walks_the_parent_links_root_first(self, tmp_path):
        self._save(tmp_path, "research", 1)
        v2 = self._save(tmp_path, "research", 2, parent="research.v1.yaml")

        assert version_chain(v2) == ("research.v1.yaml", "research.v2.yaml")

    def test_unstamped_version_is_just_its_own_name(self, tmp_path):
        version = tmp_path / "research.v1.yaml"
        save_dataset(build_dataset([_authored()]), version)  # no stamp sidecar beside it

        assert version_chain(version) == ("research.v1.yaml",)


class TestVersionSuffix:
    """`strip_version` / `version_of` are the one inverse of the `.v{N}` chain convention."""

    def test_strip_version_recovers_the_logical_name(self):
        assert strip_version("p6smoke.v2") == "p6smoke"

    def test_version_of_reads_the_ordinal(self):
        assert version_of("p6smoke.v2") == 2

    def test_unversioned_stem_is_its_own_logical_name(self):
        assert strip_version("seed") == "seed"

    def test_unversioned_stem_has_no_ordinal(self):
        assert version_of("seed") is None

    def test_zero_padded_ordinal_reads_numerically(self):
        assert strip_version("x.v03") == "x"
        assert version_of("x.v03") == 3

    def test_non_numeric_suffix_is_left_untouched(self):
        assert strip_version("x.vNaN") == "x.vNaN"
        assert version_of("x.vNaN") is None


def test_round_trip_preserves_cases_and_evaluator(tmp_path) -> None:
    path = tmp_path / "golden.yaml"
    original = build_dataset(
        [
            _authored(question="Q1", answer="A1", source_ids=("paper-a.r1", "paper-a.r2"), paper_id="paper-a"),
            _authored(question="Q2", task="benchmark", answer="A2", source_ids=("paper-b.r1",), paper_id="paper-b", difficulty="medium"),
        ]
    )

    save_dataset(original, path)
    reloaded = load_dataset(path)

    assert path.exists()
    assert len(reloaded.cases) == 2

    by_name = {case.name: case for case in reloaded.cases}
    assert {case.name for case in original.cases} == set(by_name)

    for original_case in original.cases:
        assert original_case.name is not None
        reloaded_case = by_name[original_case.name]
        assert reloaded_case.inputs == original_case.inputs
        assert reloaded_case.metadata == original_case.metadata
        assert isinstance(reloaded_case.expected_output, RetrievalAnswer)
        assert original_case.expected_output is not None
        assert reloaded_case.expected_output.cited_source_ids == original_case.expected_output.cited_source_ids
        assert reloaded_case.expected_output.answer == original_case.expected_output.answer

    # The custom evaluator survives the YAML round-trip (resolved via custom_evaluator_types).
    assert any(isinstance(evaluator, RetrievalMetrics) for evaluator in reloaded.evaluators)


def test_save_dataset_writes_yaml_loadable_without_custom_registration_failing(tmp_path) -> None:
    # A second save/load cycle from the *reloaded* dataset must also be lossless — guards against a
    # save path that silently drops the evaluator (which would then vanish on the next reload).
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset([_authored()]), path)
    once = load_dataset(path)

    again_path = tmp_path / "golden-again.yaml"
    save_dataset(once, again_path)
    twice = load_dataset(again_path)

    assert any(isinstance(evaluator, RetrievalMetrics) for evaluator in twice.evaluators)
    assert twice.cases[0].inputs == once.cases[0].inputs


class _FakeLogfireDatasetClient:
    """A recording `LogfireDatasetClient`: captures each push for assertions (no network)."""

    def __init__(self) -> None:
        self.pushed: list[tuple[object, str | None]] = []

    def push_dataset(self, dataset: object, *, name: str | None = None) -> object:
        self.pushed.append((dataset, name))
        return {"name": "stub"}


class TestLogfirePush:
    def test_push_forwards_the_dataset_to_the_client(self):
        dataset = build_dataset([_authored()])
        client = _FakeLogfireDatasetClient()

        push_dataset_to_logfire(dataset, client=client)

        assert len(client.pushed) == 1
        pushed_dataset, pushed_name = client.pushed[0]
        assert pushed_dataset is dataset
        assert pushed_name is None

    def test_build_client_constructs_a_logfire_api_client(self):
        from core.settings import LogfireSettings
        from logfire.experimental.api_client import LogfireAPIClient

        client = build_logfire_dataset_client(LogfireSettings(write_token="w", read_token="r", base_url="https://logfire-eu.pydantic.dev"))

        assert isinstance(client, LogfireAPIClient)


def test_a_case_with_no_anchor_page_needs_a_name():
    expected = ExpectedQa(answer="", expected_source_ids=("d1",))

    with pytest.raises(ValueError, match="name"):
        to_case("Q?", expected, QaCaseMeta(judgments={"d1": 1}))

    assert to_case("Q?", expected, QaCaseMeta(judgments={"d1": 1}), name="q1").name == "q1"
