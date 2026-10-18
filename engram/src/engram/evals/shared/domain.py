"""The evals value objects: the question and answer case shapes, the agent's structured output and the structural seeds.

Every model is frozen and does no I/O. `QaInput`, `ExpectedQa` and `QaCaseMeta` describe one case.
`RetrievalAnswer` is what the agent under test answers, and its ordered `cited_source_ids` feed recall, precision and MRR.
`Archetype`, `Seed`, `AnchorHash` and `derive_difficulty` describe how a generated case is seeded and banded.
"""

from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

QaDifficulty = Literal["easy", "medium", "hard"]
"""The difficulty band of a case, derived from the structural shape of its seed."""

RatedDifficulty = Literal["easy", "medium", "hard", "unrated"]
"""A difficulty as a report groups cases by it: the case's own band, or `unrated` for a case that has none."""


class DifficultyThresholds(BaseModel):
    """The tunable cutoffs `derive_difficulty` bands a case by. The defaults are the standard research bands."""

    model_config = ConfigDict(frozen=True)

    single_source: int = 1
    deep_path_hops: int = 2
    wide_fanout_sources: int = 4
    cross_section_sources: int = 2
    cross_section_sections: int = 3


DEFAULT_DIFFICULTY_THRESHOLDS = DifficultyThresholds()


class Archetype(StrEnum):
    """The structural archetype a generated eval case is seeded from (its retrieval shape).

    Each archetype names how the gold sources are found in the indexed knowledge base, which fixes the
    retrieval surface the case exercises and the difficulty band it falls in (via
    `derive_difficulty`).
    """

    PINPOINT = "pinpoint"
    SURVEY = "survey"
    TRACE = "trace"
    SYNTHESIS = "synthesis"
    PASSAGE = "passage"


def derive_difficulty(
    n_sources: int, n_hops: int, n_sections: int, thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS
) -> QaDifficulty:
    """Derive the difficulty band from a seed's structural shape.

    A single source with no hop is `easy`. Two or more hops, a wide fan-out, or a multi-source case
    that also spans several sections is `hard`. Everything else is `medium`.

    Args:
        n_sources: The number of distinct gold source ids the case relies on.
        n_hops: The number of graph hops the seed traverses (0 for non-graph archetypes).
        n_sections: The number of distinct sections the gold sources span.
        thresholds: The tunable band cutoffs, defaulting to the standard research bands.

    Returns:
        The derived `easy` / `medium` / `hard` band.
    """
    if n_sources <= thresholds.single_source and not n_hops:
        return "easy"
    deep_path = n_hops >= thresholds.deep_path_hops
    wide_fanout = n_sources >= thresholds.wide_fanout_sources
    cross_section = n_sources >= thresholds.cross_section_sources and n_sections >= thresholds.cross_section_sections
    if deep_path or wide_fanout or cross_section:
        return "hard"
    return "medium"


class AnchorHash(BaseModel):
    """A generated case's source-provenance anchor: a source unit id and its body hash."""

    model_config = ConfigDict(frozen=True)

    unit_id: str
    body_hash: str


class QaInput(BaseModel):
    """The agent-under-test's input: a question plus an optional retrieval task hint."""

    model_config = ConfigDict(frozen=True)

    question: str
    task: str | None = None  # optional retrieval task hint, as `studio search --task` takes


class ExpectedQa(BaseModel):
    """The ground truth for a QA case: the reference answer and the ids recall is scored against."""

    model_config = ConfigDict(frozen=True)

    answer: str
    expected_source_ids: tuple[str, ...]  # page ids (primary)


class QaCaseMeta(BaseModel):
    """A QA case's provenance: its anchor page and difficulty when generated, its relevance judgments when human-judged."""

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    anchor_page_id: str | None = Field(
        default=None, validation_alias=AliasChoices("anchor_page_id", "paper_id"), serialization_alias="anchor_page_id"
    )
    """The id of the anchor page, None for a human-judged case.

    Dataset files carry it under `anchor_page_id`, and a file that says `paper_id` still loads."""
    difficulty: QaDifficulty | None = None
    """The case's difficulty band, None for a human-judged case."""
    judgments: dict[str, int] = Field(default_factory=dict)
    """Each judged document's relevance judgment by id, 0 meaning not relevant. Empty for a generated case."""
    archetype: Archetype | None = None
    anchors: tuple[AnchorHash, ...] = ()
    mirror_hash: str | None = None  # content hash of the drafting text, None for a case drafted without one
    query_type: str | None = None
    """The query type a judgments file gave the case, None when it gave none."""


class RetrievalAnswer(BaseModel):
    """The agent-under-test's structured output, `cited_source_ids` is the ordered page ids used for recall@k."""

    model_config = ConfigDict(frozen=True)

    answer: str
    cited_source_ids: list[str]  # ordered page ids the agent relied on


class Seed(BaseModel):
    """A structural seed for one generated case: gold sources known by construction plus context."""

    model_config = ConfigDict(frozen=True)

    archetype: Archetype
    gold_ids: tuple[str, ...]
    source_bodies: tuple[str, ...]
    seed_key: str
    n_hops: int = 0
    n_sections: int = 1
    anchors: tuple[AnchorHash, ...] = ()

    @property
    def n_sources(self) -> int:
        """The number of distinct gold source ids the case relies on."""
        return len(self.gold_ids)

    @property
    def difficulty(self) -> QaDifficulty:
        """The difficulty band derived from this seed's structural shape."""
        return derive_difficulty(self.n_sources, self.n_hops, self.n_sections)
