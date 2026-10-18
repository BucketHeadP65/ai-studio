"""The evaluation dataset shapes, the mapping of an authored case onto a pydantic-evals case, and the version file names.

A case stores its expected page ids in the same `RetrievalAnswer` shape the evaluator scores:
`ExpectedQa.expected_source_ids` becomes `Case.expected_output.cited_source_ids`, and the authored `answer`
becomes `RetrievalAnswer.answer`.
"""

from __future__ import annotations

import re

from collections.abc import Mapping
from pathlib import PurePath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_evals import Case, Dataset

from engram.evals.harness.contracts import RetrievalMetrics
from engram.evals.shared.contracts import ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_DATASET_NAME = "engram-retrieval-golden"

# The characters a knowledge base or dataset chain name may carry inside a file name: letters, digits, dot,
# underscore and hyphen, never a path separator.
_SLUG = re.compile(r"^[A-Za-z0-9._-]+$")


def require_slug(value: str, *, source: str) -> str:
    """Check that a knowledge base or dataset chain name is safe to put inside a file name.

    The name goes into file names such as `{collection}.coverage.yaml`, `{name}.v*.yaml` and
    `{name}.frozen.current`.

    Args:
        value: The candidate name.
        source: What the value is, for the error message.

    Returns:
        The value unchanged, when it is a plain slug.

    Raises:
        ValueError: When the value carries characters outside `[A-Za-z0-9._-]` or is a dot-directory.
    """
    if not _SLUG.fullmatch(value) or value in {".", ".."}:
        msg = f"{source} must be a plain name (letters, digits, '.', '_', '-'), got {value!r}"
        raise ValueError(msg)
    return value


def require_basename(value: str, *, source: str) -> str:
    """Check that a value read back from disk is a bare file name before it is joined to a directory.

    Use it on a `.current` pointer's body and on a stamp's `parent_version`, which are always written as
    bare file names.

    Args:
        value: The candidate path component.
        source: What the value is, for the error message.

    Returns:
        The value unchanged, when it is a single, non-dot path component.

    Raises:
        ValueError: When the value is empty, absolute, a dot-directory, or spans directories.
    """
    # `PurePath("..").name` is `..`, so the dot names need their own check beside the one-component check.
    if not value or value in {".", ".."} or PurePath(value).name != value:
        msg = f"{source} must be a bare file name, got {value!r}"
        raise ValueError(msg)
    return value


# The pydantic-evals case and dataset types over the evaluation input, answer and metadata types.
EvaluationCase = Case[QaInput, RetrievalAnswer, QaCaseMeta]
EvaluationDataset = Dataset[QaInput, RetrievalAnswer, QaCaseMeta]


class AuthoredCase(BaseModel):
    """One authored case: the question, its ground truth, and its provenance.

    `build_dataset` maps it onto a pydantic-evals `Case`.

    Attributes:
        question: The agent-under-test's input.
        expected: The ground truth in the authoring shape (carries the page ids recall is scored against).
        meta: The case's provenance and its closed-set `difficulty` label.
        case_name: The stable seed-derived name `{seed_key}#{ordinal}`, or None for a case named by
            position. It stays with the case through deduplication and stratification, and
            `build_dataset` uses it in place of the positional name.
    """

    model_config = ConfigDict(frozen=True)

    question: QaInput
    expected: ExpectedQa
    meta: QaCaseMeta
    case_name: str | None = None


def to_case(question: QaInput | str, expected: ExpectedQa, meta: QaCaseMeta, *, index: int = 0, name: str | None = None) -> EvaluationCase:
    """Map an authoring triple onto a pydantic-evals `Case`, applying the expected-output contract.

    The ground-truth `ExpectedQa` is folded into the same `RetrievalAnswer` shape the evaluator scores:
    its `expected_source_ids` (page ids) become the case's `expected_output.cited_source_ids` and its
    `answer` becomes `expected_output.answer`.

    Args:
        question: The agent input, either a `QaInput` or a bare question string (wrapped in a `QaInput`
            with no task hint).
        expected: The authoring-side ground truth to fold into the scored shape.
        meta: The case's provenance and labels, stored verbatim as the case metadata.
        index: The case's position within its dataset, used to disambiguate the stable case name
            when several cases share an `anchor_page_id`.
        name: An authoritative seed-derived case name that wins over the positional `anchor_page_id`/`index`
            derivation, or None for the positional fallback.

    Returns:
        A `Case` whose `inputs` is the (normalized) question, whose `expected_output` is the
        contract-mapped `RetrievalAnswer`, and whose `metadata` is `meta`.

    Raises:
        ValueError: When `name` is None and `meta` has no anchor page to name the case after.
    """
    if name is not None:
        case_name = name
    elif meta.anchor_page_id is not None:
        case_name = _case_name(meta.anchor_page_id, index)
    else:
        msg = "a case with no anchor page needs a name"
        raise ValueError(msg)
    inputs = question if isinstance(question, QaInput) else QaInput(question=question)
    expected_output = RetrievalAnswer(answer=expected.answer, cited_source_ids=list(expected.expected_source_ids))
    return Case(name=case_name, inputs=inputs, expected_output=expected_output, metadata=meta)


def build_dataset(records: Iterable[AuthoredCase]) -> EvaluationDataset:
    """Build a pydantic-evals `Dataset` from authored cases, with the retrieval evaluator attached.

    Each record is mapped through `to_case`, with its enumeration index threaded in so cases sharing a
    `anchor_page_id` get distinct, stable names. A single `RetrievalMetrics` is attached as the dataset-level
    evaluator so it scores every case.

    Args:
        records: The authored cases, in the order they should appear in the dataset.

    Returns:
        A `Dataset` of mapped cases with a `RetrievalMetrics` evaluator.
    """
    cases = [
        to_case(record.question, record.expected, record.meta, index=index, name=record.case_name) for index, record in enumerate(records)
    ]
    # Build through the parametrized alias. A bare `Dataset(...)` serializes its types as `Any` and warns.
    return EvaluationDataset(name=_DATASET_NAME, cases=cases, evaluators=[RetrievalMetrics()])


def case_name(seed_key: str, draft_ordinal: int) -> str:
    """Build a stable, seed-derived case name.

    The name is the seed's unique key and the draft's ordinal within that seed, joined by a `#`. It
    stays the same across inserts, retirements and versions, and splits back at its last `#`.

    Args:
        seed_key: The seed the case was drafted from.
        draft_ordinal: The draft's zero-based position within that seed's drafts.

    Returns:
        A `"{seed_key}#{draft_ordinal}"` name.
    """
    return f"{seed_key}#{draft_ordinal}"


class RetirementRecord(BaseModel):
    """One case a dataset version retired, with the reason.

    A case retires when a top-up draws seeds again over a changed knowledge base and no longer draws its
    seed (`displaced`), when its gold pages have left the knowledge base (`gold_gone`), or when a freshness
    check finds its anchored sources drifted (`stale`).

    Attributes:
        case_name: The stable `{seed_key}#{draft_ordinal}` name of the retired case.
        seed_key: The seed the retired case was drafted from.
        reason: Why the case retired, `displaced` by a newcomer, its gold pages `gold_gone`, or its
            anchored sources `stale` under the live knowledge base.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    seed_key: str
    reason: Literal["displaced", "gold_gone", "stale"]


class DatasetStamp(BaseModel):
    """The immutable sidecar identity of one dataset version, everything needed to reproduce it.

    It is saved as `<name>.v{N}.stamp.json` beside the version's YAML, so a run that recorded its version
    can re-run exactly that baseline even after a later top-up.

    Attributes:
        salt: The sampling salt the version was drawn with, so the draw stays reproducible.
        knowledge_base_fingerprint: The identity of the indexed knowledge base the version was generated
            against: the `kb.index_fingerprint` hash plus the knowledge base's name.
        budget: The case budget the version targeted.
        parent_version: The prior version this one was topped up from, or None for a fresh `v1`.
        coverage: The planned quota per stratum, keyed by its `archetype/difficulty/anchor-page-type`
            label. It is the allocation the draw targeted, not a count of the cases the quote check
            kept, which a coverage audit reports.
        retired: The retirement record: the cases dropped from the parent version.
        held_out_case_ids: The case names in this version sealed as independently withheld. Empty for
            an older version or one with no held-out claim.
        independent_holdout_verified: The last custody audit's verdict on this version's held-out
            claims, or None for an older version or one never audited.
    """

    model_config = ConfigDict(frozen=True)

    salt: str
    knowledge_base_fingerprint: str
    budget: int
    parent_version: str | None = None
    coverage: Mapping[str, int]
    retired: tuple[RetirementRecord, ...] = ()
    held_out_case_ids: tuple[str, ...] = ()
    independent_holdout_verified: bool | None = None


# The trailing `.v{N}` of a versioned file stem, such as `research.v2`.
_VERSION_SUFFIX = re.compile(r"\.v(\d+)$")


def strip_version(stem: str) -> str:
    """The chain's logical name: the stem with its immutable `.v{N}` suffix stripped.

    Args:
        stem: A dataset or report file stem, versioned (`research.v2`) or not (`research`).

    Returns:
        The stem with a trailing `.v{N}` removed, or the stem unchanged when it carries none.
    """
    return _VERSION_SUFFIX.sub("", stem)


def version_of(stem: str) -> int | None:
    """The `.v{N}` ordinal of a version stem, or None for an unversioned name.

    Args:
        stem: A dataset or report file stem, versioned (`research.v2`) or not (`research`).

    Returns:
        The integer version ordinal, or None when the stem carries no `.v{N}` suffix.
    """
    match = _VERSION_SUFFIX.search(stem)
    return int(match.group(1)) if match is not None else None


def dataset_version_path(datasets_dir: Path, name: str, version: int) -> Path:
    """Resolve the YAML path of one immutable dataset version.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name (for example `research`).
        version: The 1-based version number.

    Returns:
        The `<datasets_dir>/<name>.v{version}.yaml` path.

    Raises:
        ValueError: When `name` is not a plain file-name slug.
    """
    return datasets_dir / f"{require_slug(name, source='the dataset chain name')}.v{version}.yaml"


def stamp_path(dataset_path: Path) -> Path:
    """Resolve the JSON stamp sidecar path for a dataset version's YAML path.

    Args:
        dataset_path: The version's YAML path (for example `research.v2.yaml`).

    Returns:
        The sibling `<name>.v{N}.stamp.json` path (for example `research.v2.stamp.json`).
    """
    return dataset_path.with_name(f"{dataset_path.stem}.stamp.json")


def _case_name(page_id: str, index: int) -> str:
    """The positional case name `{page_id}-{index}`."""
    return f"{page_id}-{index}"
