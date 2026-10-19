"""The freshness check's verdicts: each dataset case scored against a snapshot of the knowledge base as it is now.

The snapshot holds the current page ids, the page body hashes and the recomputed drafting text hashes. The same
snapshot always gives the same verdicts. A case's `AnchorEra` follows from the provenance it carries, and decides
which verdicts apply:

* `unanchored` cases carry no anchors and a positional name with no parseable seed key, so their only verdict
  is `unanchored`.
* `page_anchored` cases carry per-page `AnchorHash`es but no drafting text hash, so a page whose card text moved
  flags `page_drifted`.
* `mirror_anchored` cases also carry a `mirror_hash` of the exact text their support quotes were checked against,
  so a changed drafting text flags `mirror_stale` even when the page cards are untouched.

`retiring_names` and `staleness_fraction` read each verdict by its case's era: a `page_drifted` mirror-era case
whose drafting text still recomputes intact is reported and never retired.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.seed_shapes import MIN_SURVEY_FANOUT, MIN_SYNTHESIS_FANOUT
from engram.evals.shared.contracts import AnchorHash, Archetype

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from engram.evals.generation.domain.datasets import EvaluationCase

FreshnessVerdict = Literal["fresh", "page_drifted", "mirror_stale", "gold_gone", "unanchored"]
AnchorEra = Literal["unanchored", "page_anchored", "mirror_anchored"]

# A seed-derived case name is `{seed_key}#{draft_ordinal}`. A positional name, `{anchor_page_id}-{index}`, has no `#`.
_ORDINAL_SEPARATOR = "#"

# The verdicts in report order, spelled out because `typing.get_args` would type them as `Any`.
_ALL_VERDICTS: tuple[FreshnessVerdict, ...] = ("fresh", "page_drifted", "mirror_stale", "gold_gone", "unanchored")


class CaseFreshnessRef(BaseModel):
    """The part of one dataset case the freshness verdicts need.

    `case_freshness_refs` builds these from a loaded dataset's cases.

    Attributes:
        case_name: The case's name, its identity in the report and the retirement record.
        seed_key: The seed the case was drafted from, or None for a positional name.
        archetype: The case's structural archetype, or None for an unlabelled case.
        gold_ids: The case's gold page ids, checked against the knowledge base for `gold_gone` attribution.
        anchors: The case's per-page provenance anchors (empty for an unanchored case).
        mirror_hash: The drafting-text hash for a mirror-era case, None for a page-anchored or unanchored one.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    seed_key: str | None
    archetype: Archetype | None
    gold_ids: tuple[str, ...]
    anchors: tuple[AnchorHash, ...]
    mirror_hash: str | None


class CaseFreshness(BaseModel):
    """One case's freshness verdict against a knowledge base snapshot.

    Attributes:
        case_name: The scored case's name.
        era: The case's anchor era, which authorizes what verdicts and retirement apply.
        verdict: The freshness verdict, the case's most severe reason under the precedence.
        moved_page_ids: The anchor unit ids (or missing gold ids) that drifted or vanished, for
            per-page attribution. Empty when nothing was detectable.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    era: AnchorEra
    verdict: FreshnessVerdict
    moved_page_ids: tuple[str, ...]


class CasePremise(BaseModel):
    """One case's structural premise check, which is only reported and never a verdict or a retirement.

    A seed-derived case encodes the graph structure it was drawn from in its seed key (a trace path, a
    survey hub, a synthesis fan). The check recomputes that basis against the live `kb.edges`. A broken
    premise never moves a freshness verdict.

    Attributes:
        case_name: The checked case's name.
        archetype: The case's structural archetype (the premise's shape).
        intact: Whether every structural requirement the seed encoded still holds.
        missing: Human-readable descriptions of the broken requirements (empty when intact), for
            example `edge a->b gone` or `fan-in 2 < threshold 3`.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    archetype: Archetype
    intact: bool
    missing: tuple[str, ...]


class FreshnessReport(BaseModel):
    """The freshness check's report for one dataset version against one knowledge base.

    Attributes:
        collection: The checked knowledge base's name.
        dataset_version: The checked dataset version's identifier.
        verdicts: The per-case verdicts, in dataset order.
        counts: The number of cases per verdict (every verdict present, zeros included).
        staleness_fraction: The stale fraction over the cases that can take a verdict, judged by each case's era.
        stale_by_page: A page id to the number of cases that page invalidated (its attribution tally).
        structural: The per-case structural premises, which are only reported. Empty when none were checked.
        n_premise_broken: How many checked premises are broken.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    dataset_version: str
    verdicts: tuple[CaseFreshness, ...]
    counts: Mapping[FreshnessVerdict, int]
    staleness_fraction: float
    stale_by_page: Mapping[str, int]
    structural: tuple[CasePremise, ...]
    n_premise_broken: int


def case_era(ref: CaseFreshnessRef) -> AnchorEra:
    """Classify a case's anchor era from the provenance it carries.

    Args:
        ref: The case reference to classify.

    Returns:
        `mirror_anchored` when a `mirror_hash` is present, `unanchored` when the case carries neither
        anchors nor a parseable seed key, and `page_anchored` otherwise.
    """
    if ref.mirror_hash is not None:
        return "mirror_anchored"
    if not ref.anchors and ref.seed_key is None:
        return "unanchored"
    return "page_anchored"


def evaluate_case(
    ref: CaseFreshnessRef, *, current_ids: frozenset[str], current_hashes: Mapping[str, str], recomputed_mirror_hash: str | None
) -> CaseFreshness:
    """Score one case against a knowledge base snapshot, applying the verdict precedence.

    The precedence is `unanchored` then `gold_gone` then `mirror_stale` (mirror era) then
    `page_drifted` then `fresh`, so a case stale on several axes reports its most severe reason. A
    mirror-era case whose drafting text could not be recomputed (`recomputed_mirror_hash` is None) is
    `mirror_stale` even when its gold pages are intact.

    Args:
        ref: The case reference to score.
        current_ids: The knowledge base's current page ids (a gold id absent from it is `gold_gone`).
        current_hashes: The current page-id to card body hash mapping (an anchor whose stored body
            hash differs, or whose page vanished, is drift).
        recomputed_mirror_hash: The hash of the case's drafting text recomputed from the current knowledge
            base, or None when it could not be rebuilt. Read only for mirror-era cases.

    Returns:
        The case's `CaseFreshness`, with `moved_page_ids` attributing any detectable drift or absence.
    """
    era = case_era(ref)
    if era == "unanchored":
        return CaseFreshness(case_name=ref.case_name, era=era, verdict="unanchored", moved_page_ids=())
    missing = tuple(gold_id for gold_id in ref.gold_ids if gold_id not in current_ids)
    if missing:
        return CaseFreshness(case_name=ref.case_name, era=era, verdict="gold_gone", moved_page_ids=missing)
    drifted = tuple(anchor.unit_id for anchor in ref.anchors if anchor.body_hash != current_hashes.get(anchor.unit_id))
    if era == "mirror_anchored" and (recomputed_mirror_hash is None or recomputed_mirror_hash != ref.mirror_hash):
        return CaseFreshness(case_name=ref.case_name, era=era, verdict="mirror_stale", moved_page_ids=drifted)
    if drifted:
        return CaseFreshness(case_name=ref.case_name, era=era, verdict="page_drifted", moved_page_ids=drifted)
    return CaseFreshness(case_name=ref.case_name, era=era, verdict="fresh", moved_page_ids=())


def evaluate_freshness(
    refs: Iterable[CaseFreshnessRef],
    *,
    current_ids: frozenset[str],
    current_hashes: Mapping[str, str],
    recomputed: Mapping[str, str | None],
) -> tuple[CaseFreshness, ...]:
    """Score every case reference against one knowledge base snapshot.

    Args:
        refs: The case references to score (from `case_freshness_refs`).
        current_ids: The knowledge base's current page ids.
        current_hashes: The current page-id to card body hash mapping.
        recomputed: A case-name to recomputed drafting-text hash mapping (None on a recompute
            failure). Non-mirror-era cases need no entry, their recomputed hash is never read.

    Returns:
        One `CaseFreshness` per reference, in input order.
    """
    return tuple(
        evaluate_case(ref, current_ids=current_ids, current_hashes=current_hashes, recomputed_mirror_hash=recomputed.get(ref.case_name))
        for ref in refs
    )


def staleness_fraction(verdicts: Sequence[CaseFreshness]) -> float:
    """The fraction of the cases that can take a verdict which are stale by their era's rule.

    `unanchored` cases are left out of the denominator. The numerator is the retiring set
    (`gold_gone`, `mirror_stale` and page-anchored `page_drifted`), so a mirror-era drift that is only
    reported never raises the fraction.

    Args:
        verdicts: The scored cases.

    Returns:
        The stale fraction, or 0.0 when no case can take a verdict.
    """
    verdictable = [verdict for verdict in verdicts if verdict.verdict != "unanchored"]
    if not verdictable:
        return 0.0
    stale = sum(1 for verdict in verdictable if _is_retiring(verdict))
    return stale / len(verdictable)


def retiring_names(verdicts: Sequence[CaseFreshness]) -> frozenset[str]:
    """The names of the cases a top-up should retire, judged by each case's era.

    `gold_gone` always retires, `mirror_stale` retires its mirror-era case, and `page_drifted` retires
    only a page-anchored case (an intact-mirror drift on a mirror-era case is report-only).
    `unanchored` and `fresh` never retire.

    Args:
        verdicts: The scored cases.

    Returns:
        The names of the cases to retire.
    """
    return frozenset(verdict.case_name for verdict in verdicts if _is_retiring(verdict))


def case_freshness_refs(cases: Iterable[EvaluationCase]) -> list[CaseFreshnessRef]:
    """Extract a `CaseFreshnessRef` from each of a loaded dataset's cases.

    Unanchored and unlabelled cases are kept. A case with no name is skipped, since no report could identify it.

    Args:
        cases: The dataset's cases (each an `EvaluationCase` carrying a name and optional metadata).

    Returns:
        One `CaseFreshnessRef` per named case, in input order.
    """
    refs: list[CaseFreshnessRef] = []
    for case in cases:
        if case.name is None:
            continue
        meta = case.metadata
        gold = tuple(case.expected_output.cited_source_ids) if case.expected_output is not None else ()
        refs.append(
            CaseFreshnessRef(
                case_name=case.name,
                seed_key=_seed_key_of(case.name),
                archetype=meta.archetype if meta is not None else None,
                gold_ids=gold,
                anchors=meta.anchors if meta is not None else (),
                mirror_hash=meta.mirror_hash if meta is not None else None,
            )
        )
    return refs


def build_report(
    collection: str, dataset_version: str, verdicts: Sequence[CaseFreshness], *, premises: Sequence[CasePremise] = ()
) -> FreshnessReport:
    """Assemble the freshness report from scored verdicts.

    Args:
        collection: The checked knowledge base's name.
        dataset_version: The checked dataset version's identifier.
        verdicts: The scored cases.
        premises: The per-case structural premises, empty when none were checked. They never change a
            verdict or the staleness fraction.

    Returns:
        The `FreshnessReport` with per-verdict counts, the staleness fraction, and the per-page
        invalidation tally, plus the structural premise section.
    """
    return FreshnessReport(
        collection=collection,
        dataset_version=dataset_version,
        verdicts=tuple(verdicts),
        counts=_verdict_counts(verdicts),
        staleness_fraction=staleness_fraction(verdicts),
        stale_by_page=_stale_by_page(verdicts),
        structural=tuple(premises),
        n_premise_broken=sum(1 for premise in premises if not premise.intact),
    )


def _is_retiring(verdict: CaseFreshness) -> bool:
    """Whether a verdict retires its case under its era's rule, which also decides the stale set."""
    if verdict.verdict in {"gold_gone", "mirror_stale"}:
        return True
    if verdict.verdict == "page_drifted":
        return verdict.era == "page_anchored"
    return False


def is_audit_invalidating(verdict: CaseFreshness) -> bool:
    """Whether an external custody audit must treat this verdict as invalidating its case outright.

    It follows the rule a top-up retires by: `gold_gone` and `mirror_stale` invalidate under every era,
    and `page_drifted` invalidates only a page-anchored case.

    Args:
        verdict: The case's scored freshness verdict.

    Returns:
        True when the verdict makes the case's label untrustworthy for this audit cycle.
    """
    return _is_retiring(verdict)


def _verdict_counts(verdicts: Sequence[CaseFreshness]) -> Mapping[FreshnessVerdict, int]:
    """Count each verdict, including the ones with no cases."""
    counter = Counter(verdict.verdict for verdict in verdicts)
    return {verdict: counter.get(verdict, 0) for verdict in _ALL_VERDICTS}


def _stale_by_page(verdicts: Sequence[CaseFreshness]) -> Mapping[str, int]:
    """Tally, per page id, how many cases it invalidated (from each verdict's `moved_page_ids`)."""
    counter: Counter[str] = Counter()
    for verdict in verdicts:
        counter.update(verdict.moved_page_ids)
    return dict(counter)


def _seed_key_of(case_name: str) -> str | None:
    """The seed key before the last `#` of a seed-derived name, or None for a positional name."""
    if _ORDINAL_SEPARATOR not in case_name:
        return None
    return case_name.rsplit(_ORDINAL_SEPARATOR, 1)[0]


# -- structural premises, only reported -----------------------------------------
#
# A seed key encodes the graph structure its case was drawn from (the formats `infrastructure.seeds` writes):
# `trace:a>b>c` (a hop path), `survey:{rel}:{dst}` (an inbound fan), `synthesis:{rel}:{src}` (an outbound
# fan), `pinpoint:{claim_id}` and `passage:{section_id}` (their basis is already covered by the mirror hash).
# A broken premise is only reported. It never changes a verdict or the retiring set.

_TRACE_PREFIX = "trace:"
_SURVEY_PREFIX = "survey:"
_SYNTHESIS_PREFIX = "synthesis:"
_TRACE_HOP_SEPARATOR = ">"


class PremiseReads(BaseModel):
    """The lookup keys one premise check needs, collected from the refs in one pass.

    Attributes:
        edge_pairs: The `(src, dst)` pairs the trace premises require to exist in `kb.edges`.
        inbound_keys: The `(rel, dst)` keys the survey premises count inbound fans for.
        outbound_keys: The `(rel, src)` keys the synthesis premises count outbound fans for.
    """

    model_config = ConfigDict(frozen=True)

    edge_pairs: frozenset[tuple[str, str]]
    inbound_keys: frozenset[tuple[str, str]]
    outbound_keys: frozenset[tuple[str, str]]


def _trace_pairs(seed_key: str) -> tuple[tuple[str, str], ...]:
    """The consecutive `(src, dst)` hops of a `trace:a>b>c` seed key."""
    path = seed_key.removeprefix(_TRACE_PREFIX).split(_TRACE_HOP_SEPARATOR)
    return tuple(pairwise(path))


def _fan_key(seed_key: str, prefix: str) -> tuple[str, str] | None:
    """The `(rel, node)` key of a `survey:`/`synthesis:` seed key, or None when malformed."""
    remainder = seed_key.removeprefix(prefix)
    rel, separator, node = remainder.partition(":")
    if not separator or not rel or not node:
        return None
    return (rel, node)


def premise_reads(refs: Sequence[CaseFreshnessRef]) -> PremiseReads:
    """Collect the lookup keys the premise check needs, in one pass over the seed-keyed refs.

    Positional refs, which have no seed key, and pinpoint and passage refs, whose basis is the mirror hash, add nothing.

    Args:
        refs: The checked cases' freshness refs.

    Returns:
        The batched `PremiseReads` the reader resolves in three SELECTs at most.
    """
    edge_pairs: set[tuple[str, str]] = set()
    inbound: set[tuple[str, str]] = set()
    outbound: set[tuple[str, str]] = set()
    for ref in refs:
        key = ref.seed_key
        if key is None:
            continue
        if key.startswith(_TRACE_PREFIX):
            edge_pairs.update(_trace_pairs(key))
        elif key.startswith(_SURVEY_PREFIX) and (fan := _fan_key(key, _SURVEY_PREFIX)) is not None:
            inbound.add(fan)
        elif key.startswith(_SYNTHESIS_PREFIX) and (fan := _fan_key(key, _SYNTHESIS_PREFIX)) is not None:
            outbound.add(fan)
    return PremiseReads(
        edge_pairs=frozenset(edge_pairs),
        inbound_keys=frozenset(inbound),
        outbound_keys=frozenset(outbound),
    )


def _premise_missing(
    ref: CaseFreshnessRef,
    *,
    present_pairs: frozenset[tuple[str, str]],
    inbound_fans: Mapping[tuple[str, str], int],
    outbound_fans: Mapping[tuple[str, str], int],
) -> tuple[str, ...] | None:
    """One ref's broken structural requirements, `()` for pinpoint and passage, or None when it has no premise to check."""
    key = ref.seed_key
    if key is None:
        return None
    if ref.archetype in {Archetype.PINPOINT, Archetype.PASSAGE}:
        return ()
    if key.startswith(_TRACE_PREFIX):
        return tuple(f"edge {src}->{dst} gone" for src, dst in _trace_pairs(key) if (src, dst) not in present_pairs)
    for prefix, fans, threshold, label in (
        (_SURVEY_PREFIX, inbound_fans, MIN_SURVEY_FANOUT, "fan-in"),
        (_SYNTHESIS_PREFIX, outbound_fans, MIN_SYNTHESIS_FANOUT, "fan-out"),
    ):
        if key.startswith(prefix):
            return _fan_missing(key, prefix, fans=fans, threshold=threshold, label=label)
    return None


def _fan_missing(seed_key: str, prefix: str, *, fans: Mapping[tuple[str, str], int], threshold: int, label: str) -> tuple[str, ...] | None:
    """One fan premise's broken requirement, `()` when the live fan still clears the seeder threshold."""
    fan = _fan_key(seed_key, prefix)
    if fan is None:
        return None
    count = fans.get(fan, 0)
    return (f"{label} {count} < threshold {threshold}",) if count < threshold else ()


def evaluate_premises(
    refs: Sequence[CaseFreshnessRef],
    *,
    present_pairs: frozenset[tuple[str, str]],
    inbound_fans: Mapping[tuple[str, str], int],
    outbound_fans: Mapping[tuple[str, str], int],
) -> tuple[CasePremise, ...]:
    """Recompute every seed-keyed case's structural premise against the live knowledge base reads.

    Positional refs, unlabelled refs and unknown seed-key shapes are skipped, never guessed at. Pinpoint and
    passage cases are always intact, since the mirror hash covers their drafting text. No premise ever
    changes a freshness verdict.

    Args:
        refs: The checked cases' freshness refs, in dataset order.
        present_pairs: The `(src, dst)` pairs that exist in `kb.edges` (from the reader).
        inbound_fans: Each `(rel, dst)` key's distinct inbound source count.
        outbound_fans: Each `(rel, src)` key's distinct reached-page count.

    Returns:
        One `CasePremise` per checkable ref, in input order.
    """
    premises: list[CasePremise] = []
    for ref in refs:
        if ref.archetype is None:
            continue
        missing = _premise_missing(ref, present_pairs=present_pairs, inbound_fans=inbound_fans, outbound_fans=outbound_fans)
        if missing is None:
            continue
        premises.append(CasePremise(case_name=ref.case_name, archetype=ref.archetype, intact=not missing, missing=missing))
    return tuple(premises)


def freshness_artifacts_dir(datasets_dir: Path) -> Path:
    """The freshness-report home: the `freshness/` sibling of the datasets directory.

    Args:
        datasets_dir: The directory the dataset version chains live in.

    Returns:
        The `<datasets_dir>/../freshness` directory the freshness check writes its reports to.
    """
    return datasets_dir.parent / "freshness"


def render_freshness_markdown(report: FreshnessReport) -> str:
    """Render a `FreshnessReport` as a deterministic markdown summary, the `.freshness.md` sibling.

    The text is ASCII only and identical for identical reports.

    Args:
        report: The freshness report to render.

    Returns:
        The markdown text.
    """
    lines = [
        f"# Freshness report: {report.collection}",
        "",
        f"**Dataset version:** {report.dataset_version}",
        f"**Staleness fraction:** {report.staleness_fraction:.3f}",
        "",
        "## Verdict counts",
        "",
        *[f"- {verdict}: {report.counts.get(verdict, 0)}" for verdict in _ALL_VERDICTS],
        "",
        "## Stale by page",
        "",
        *_stale_by_page_block(report.stale_by_page),
        "",
        "## Structural integrity (report-only)",
        "",
        *_structural_block(report.structural),
    ]
    return "\n".join(lines) + "\n"


def _structural_block(premises: Sequence[CasePremise]) -> list[str]:
    """Render the structural premise section: the checked and broken tally plus one bullet per broken premise."""
    if not premises:
        return ["(none checked)"]
    broken = [premise for premise in premises if not premise.intact]
    summary = f"checked: {len(premises)}, broken: {len(broken)}"
    if not broken:
        return [summary]
    return [summary, "", *[f"- {premise.case_name} ({premise.archetype.value}): {'; '.join(premise.missing)}" for premise in broken]]


def ranked_stale_pages(stale_by_page: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Rank the per-page invalidation tally, most-invalidating first, ties broken by ascending page id.

    Args:
        stale_by_page: The page id to invalidation-count tally.

    Returns:
        The `(page_id, count)` pairs sorted by descending count, then ascending page id.
    """
    return tuple(sorted(stale_by_page.items(), key=lambda item: (-item[1], item[0])))


def _stale_by_page_block(stale_by_page: Mapping[str, int]) -> list[str]:
    """Render the per-page invalidation tally as a markdown bullet list (most-invalidating first), or `(none)`."""
    if not stale_by_page:
        return ["(none)"]
    return [f"- {page_id}: {count}" for page_id, count in ranked_stale_pages(stale_by_page)]
