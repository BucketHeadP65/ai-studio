"""Tests for the dataset-versioning + stable-case-identity primitives.

These pin down the artifact layer top-up is built on: immutable `<name>.v{N}.yaml` versions with a
JSON stamp sidecar, a one-line `<name>.current` pointer, and seed-derived `{seed_key}#{ordinal}` case
names. The stamp round-trips through `model_validate` at the JSON boundary, a dataset carrying no
stamp reads back as a clean `None`, and the case name parses back unambiguously.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import (
    AuthoredCase,
    DatasetStamp,
    build_dataset,
    dataset_version_path,
    load_dataset,
    load_stamp,
    read_current,
    save_dataset,
    save_dataset_version,
    used_case_names,
    write_current,
)
from engram.evals.generation.domain.datasets import RetirementRecord, case_name, stamp_path
from engram.evals.shared.domain import ExpectedQa, QaCaseMeta, QaInput

if TYPE_CHECKING:
    from collections.abc import Mapping


def _authored(question: str = "What is the reported accuracy?") -> AuthoredCase:
    """Build a minimal :class:`AuthoredCase` so `build_dataset` produces a persistable dataset."""
    return AuthoredCase(
        question=QaInput(question=question),
        expected=ExpectedQa(answer="94.2% on the held-out set.", expected_source_ids=("paper-a.results",)),
        meta=QaCaseMeta(anchor_page_id="paper-a", difficulty="easy"),
    )


def _stamp(
    *,
    salt: str = "s1",
    knowledge_base_fingerprint: str = "spec-abc/research",
    budget: int = 120,
    parent_version: str | None = None,
    coverage: Mapping[str, int] | None = None,
    retired: tuple[RetirementRecord, ...] = (),
    held_out_case_ids: tuple[str, ...] = (),
    independent_holdout_verified: bool | None = None,
) -> DatasetStamp:
    """Build a :class:`DatasetStamp` with sensible defaults for the tests."""
    return DatasetStamp(
        salt=salt,
        knowledge_base_fingerprint=knowledge_base_fingerprint,
        budget=budget,
        parent_version=parent_version,
        coverage=dict(coverage) if coverage is not None else {"pinpoint/easy/claim": 40},
        retired=retired,
        held_out_case_ids=held_out_case_ids,
        independent_holdout_verified=independent_holdout_verified,
    )


def _write_stamp(version_path, stamp: DatasetStamp) -> None:
    """Persist just the JSON stamp sidecar for `version_path` (no dataset YAML needed for the walk)."""
    stamp_path(version_path).write_text(stamp.model_dump_json(indent=2), encoding="utf-8")


def test_dataset_version_path_uses_a_versioned_yaml_name(tmp_path) -> None:
    assert dataset_version_path(tmp_path, "research", 3) == tmp_path / "research.v3.yaml"


def test_stamp_path_is_a_json_sidecar_of_the_dataset(tmp_path) -> None:
    dataset_path = dataset_version_path(tmp_path, "research", 2)

    assert stamp_path(dataset_path) == tmp_path / "research.v2.stamp.json"


def test_save_dataset_version_writes_the_dataset_and_its_stamp(tmp_path) -> None:
    path = dataset_version_path(tmp_path, "research", 1)
    stamp = _stamp(
        parent_version="research.v0.yaml",
        retired=(RetirementRecord(case_name="pinpoint:page.x#0", seed_key="pinpoint:page.x", reason="displaced"),),
    )

    save_dataset_version(build_dataset([_authored()]), stamp, path)

    assert path.exists()
    assert stamp_path(path).exists()
    # The version YAML still loads through the ordinary golden-dataset loader.
    assert len(load_dataset(path).cases) == 1


def test_stamp_round_trips_through_model_validate(tmp_path) -> None:
    path = dataset_version_path(tmp_path, "research", 1)
    retired = (RetirementRecord(case_name="trace:a>b#1", seed_key="trace:a>b", reason="gold_gone"),)
    stamp = _stamp(coverage={"survey/medium/paper": 7, "trace/hard/paper": 2}, retired=retired)

    save_dataset_version(build_dataset([_authored()]), stamp, path)
    loaded = load_stamp(path)

    assert isinstance(loaded, DatasetStamp)
    # A full-value round-trip: the sidecar is re-validated at the JSON boundary and is lossless.
    assert loaded == stamp
    assert loaded.coverage == {"survey/medium/paper": 7, "trace/hard/paper": 2}
    assert loaded.retired[0].reason == "gold_gone"


def test_load_stamp_returns_none_for_a_legacy_dataset(tmp_path) -> None:
    # A pre-coverage dataset has no sidecar stamp; loading its stamp is a clean None, not an error.
    path = tmp_path / "legacy.yaml"
    save_dataset(build_dataset([_authored()]), path)

    assert stamp_path(path).exists() is False
    assert load_stamp(path) is None


def test_stamp_defaults_are_a_fresh_v1(tmp_path) -> None:
    path = dataset_version_path(tmp_path, "research", 1)
    save_dataset_version(build_dataset([_authored()]), _stamp(), path)

    loaded = load_stamp(path)

    assert loaded is not None
    assert loaded.parent_version is None
    assert loaded.retired == ()


def test_stamp_defaults_governance_fields_for_a_legacy_sidecar(tmp_path) -> None:
    # A pre-governance sidecar carries neither key, so both read back as their backward-compatible default.
    path = dataset_version_path(tmp_path, "research", 1)
    legacy_json = json.dumps(
        {
            "salt": "s1",
            "knowledge_base_fingerprint": "spec-abc/research",
            "budget": 120,
            "parent_version": None,
            "coverage": {"pinpoint/easy/claim": 40},
            "retired": [],
        }
    )
    stamp_path(path).write_text(legacy_json, encoding="utf-8")

    loaded = load_stamp(path)

    assert loaded is not None
    assert loaded.held_out_case_ids == ()
    assert loaded.independent_holdout_verified is None


def test_stamp_governance_fields_round_trip(tmp_path) -> None:
    path = dataset_version_path(tmp_path, "research", 1)
    stamp = _stamp(held_out_case_ids=("pinpoint:page.x#0", "trace:a>b#1"), independent_holdout_verified=True)

    save_dataset_version(build_dataset([_authored()]), stamp, path)
    loaded = load_stamp(path)

    assert loaded is not None
    assert loaded.held_out_case_ids == ("pinpoint:page.x#0", "trace:a>b#1")
    assert loaded.independent_holdout_verified is True


def test_current_pointer_round_trips(tmp_path) -> None:
    version_path = dataset_version_path(tmp_path, "research", 2)
    write_current(tmp_path, "research", version_path)

    assert read_current(tmp_path, "research") == version_path


def test_read_current_returns_none_when_no_pointer_exists(tmp_path) -> None:
    assert read_current(tmp_path, "research") is None


def test_case_name_joins_seed_key_and_ordinal_and_parses_back() -> None:
    name = case_name("survey:builds_on:page.x", 2)

    assert name == "survey:builds_on:page.x#2"
    # The '#' separator never appears in a seed key, so the name splits back unambiguously.
    seed_key, sep, ordinal = name.rpartition("#")
    assert sep == "#"
    assert seed_key == "survey:builds_on:page.x"
    assert int(ordinal) == 2


def test_retirement_record_round_trips_reason_stale(tmp_path) -> None:
    path = dataset_version_path(tmp_path, "research", 2)
    retired = (RetirementRecord(case_name="pinpoint:page.z#0", seed_key="pinpoint:page.z", reason="stale"),)
    stamp = _stamp(parent_version="research.v1.yaml", retired=retired)

    save_dataset_version(build_dataset([_authored()]), stamp, path)
    loaded = load_stamp(path)

    assert loaded is not None
    # The widened reason survives the JSON-sidecar round-trip losslessly.
    assert loaded == stamp
    assert loaded.retired[0].reason == "stale"


def test_used_case_names_walks_the_whole_version_chain(tmp_path) -> None:
    v1 = dataset_version_path(tmp_path, "research", 1)
    v2 = dataset_version_path(tmp_path, "research", 2)
    v3 = dataset_version_path(tmp_path, "research", 3)
    _write_stamp(v1, _stamp(retired=(RetirementRecord(case_name="gone:v1#0", seed_key="gone:v1", reason="stale"),)))
    _write_stamp(
        v2, _stamp(parent_version=v1.name, retired=(RetirementRecord(case_name="gone:v2#0", seed_key="gone:v2", reason="displaced"),))
    )
    _write_stamp(v3, _stamp(parent_version=v2.name))

    used = used_case_names(v3, live_names=["live:v3#0", "live:v3#1"])

    # A name retired three versions back is still remembered, alongside the live v3 names.
    assert used == frozenset({"gone:v1#0", "gone:v2#0", "live:v3#0", "live:v3#1"})


def test_used_case_names_tolerates_a_missing_root_stamp(tmp_path) -> None:
    v1 = dataset_version_path(tmp_path, "research", 1)  # a root that predates stamps: no sidecar.
    v2 = dataset_version_path(tmp_path, "research", 2)
    _write_stamp(v2, _stamp(parent_version=v1.name, retired=(RetirementRecord(case_name="gone:v2#0", seed_key="gone:v2", reason="stale"),)))

    used = used_case_names(v2, live_names=["live:v2#0"])

    # The walk ends gracefully at the missing root, returning the union of what it could reach.
    assert used == frozenset({"gone:v2#0", "live:v2#0"})


def test_used_case_names_returns_only_live_names_for_an_unstamped_version(tmp_path) -> None:
    version_path = dataset_version_path(tmp_path, "research", 1)  # no stamp at all.

    used = used_case_names(version_path, live_names=["live:v1#0", "live:v1#1"])

    assert used == frozenset({"live:v1#0", "live:v1#1"})


def test_used_case_names_tolerates_an_unreadable_stamp(tmp_path) -> None:
    version_path = dataset_version_path(tmp_path, "research", 1)
    stamp_path(version_path).write_text("{ not valid json", encoding="utf-8")

    used = used_case_names(version_path, live_names=["live:v1#0"])

    # A malformed sidecar ends the walk gracefully rather than raising.
    assert used == frozenset({"live:v1#0"})


def test_used_case_names_guards_against_a_cyclic_parent_link(tmp_path) -> None:
    v1 = dataset_version_path(tmp_path, "research", 1)
    v2 = dataset_version_path(tmp_path, "research", 2)
    # A corrupt chain that points in a loop must not spin forever.
    _write_stamp(v1, _stamp(parent_version=v2.name, retired=(RetirementRecord(case_name="a#0", seed_key="a", reason="stale"),)))
    _write_stamp(v2, _stamp(parent_version=v1.name, retired=(RetirementRecord(case_name="b#0", seed_key="b", reason="stale"),)))

    used = used_case_names(v2, live_names=["live#0"])

    assert used == frozenset({"a#0", "b#0", "live#0"})
