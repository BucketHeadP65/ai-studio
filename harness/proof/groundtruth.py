"""The knowledge base's own questions, and the evidence each published synthesis rests on.

The research knowledge base contains six `question` pages: contested forks in the agent-memory and
retrieval literature, each one synthesised from primary sources and each one citing those
sources by link. That citation set is the groundtruth used here -- not a rubric written for
this experiment, and not a model's opinion of an answer. A retrieval run is scored on whether
it surfaced the evidence the published synthesis stands on.

Two properties make this honest, and both are worth stating where a reader meets them:

* **No model appears in the metric.** Recall is set arithmetic over page ids. Nothing scores
  prose, so nothing can be talked into a better score.
* **The question page is withheld from every run.** All six are live in the knowledge base, so a run
  allowed to read them would retrieve the finished answer and score perfectly while proving
  nothing. `WITHHELD` is what both paths exclude, and excluding it is what makes the task the
  reconstruction it claims to be.

Citations are read from the exported bundle -- the same pages, in git, diffable -- and every id
they resolve to is checked against the live knowledge base before use, so a renamed page fails loudly
here instead of quietly deflating every score downstream.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

BUNDLE = Path(__file__).resolve().parents[2] / "engram" / "bundles" / "research"
QUESTIONS = BUNDLE / "questions"

_LINK = re.compile(r"\(/(papers|entities)/([A-Za-z0-9._-]+)\.md[^)]*\)")
_PREFIX = {"papers": "paper", "entities": "ent"}
"""How a bundle path names a page versus how the knowledge base does: `/papers/x.md` is `paper.x`."""

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_TITLE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ResearchQuestion:
    """A contested fork the knowledge base has already answered, and the pages that answer it.

    Attributes:
        id: The question page's knowledge base id (`q.` prefixed). Withheld from retrieval.
        question: The fork, as the knowledge base states it -- the prompt both paths are given.
        sources: Knowledge base ids of the papers and entities the published synthesis cites.
    """

    id: str
    question: str
    sources: frozenset[str]

    def recall(self, surfaced: Iterable[str]) -> float:
        """Fraction of the cited evidence a run surfaced.

        Args:
            surfaced: Knowledge base ids the run touched. Section ids (`paper.x#section`) count for
                their page, because reading a section is reading the paper.
        """
        if not self.sources:
            return 1.0
        pages = {str(item).split("#", 1)[0] for item in surfaced}
        return len(self.sources & pages) / len(self.sources)

    def missed(self, surfaced: Iterable[str]) -> frozenset[str]:
        """Cited evidence the run never reached -- the half of a score worth reading."""
        pages = {str(item).split("#", 1)[0] for item in surfaced}
        return self.sources - pages


def _question(path: Path) -> ResearchQuestion:
    text = path.read_text(encoding="utf-8")
    front = _FRONTMATTER.match(text)
    if front is None:
        msg = f"{path.name} has no frontmatter, so it declares no title"
        raise ValueError(msg)
    title = _TITLE.search(front.group(1))
    if title is None:
        msg = f"{path.name} declares no title"
        raise ValueError(msg)
    body = text[front.end() :]
    sources = frozenset(f"{_PREFIX[kind]}.{slug}" for kind, slug in _LINK.findall(body))
    return ResearchQuestion(id=f"q.{path.stem}", question=title.group(1).strip("'\""), sources=sources)


def load_questions() -> list[ResearchQuestion]:
    """Every question page in the bundle, ordered by id so a run is reproducible."""
    return sorted((_question(path) for path in QUESTIONS.glob("*.md")), key=lambda q: q.id)


WITHHELD: tuple[str, ...] = tuple(f"q.{path.stem}" for path in sorted(QUESTIONS.glob("*.md")))
"""Every question page, excluded from retrieval on both paths.

Not only the one being asked: leaving the other five reachable lets a run find a sibling
synthesis that cites the same primary sources, which is the same shortcut wearing a hat.
"""


def unresolved(questions: Sequence[ResearchQuestion], known: Iterable[str]) -> dict[str, frozenset[str]]:
    """Cited ids that no live knowledge base page answers to, per question.

    A non-empty result means the bundle and the knowledge base disagree about what a page is called.
    Every score computed against a stale id is silently too low, so this is checked before an
    experiment runs rather than reconciled after it.
    """
    live = set(known)
    return {q.id: missing for q in questions if (missing := q.sources - live)}
