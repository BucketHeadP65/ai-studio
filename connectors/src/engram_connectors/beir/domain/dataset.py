"""A BEIR dataset's three files as values: its documents, its queries and its relevance judgments.

`corpus.jsonl` holds one `{"_id", "title", "text"}` object per line, `queries.jsonl` one `{"_id", "text"}` object
per line, and `qrels/<split>.tsv` a header line then `query-id`, `corpus-id` and `score` per line, tab-separated.
"""

from __future__ import annotations

import json
import re

from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from engram_connectors.beir.exceptions import BeirDatasetError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_SAFE_FILE_STEM = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class BeirDocument(BaseModel):
    """One document of a benchmark dataset."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    title: str = ""
    text: str = ""

    @property
    def display_title(self) -> str:
        """The title, or the document id when the title is empty."""
        return self.title.strip() or self.id

    def markdown(self) -> str:
        """The mirror text: the display title as a heading, a blank line, then the text."""
        return f"# {self.display_title}\n\n{self.text.strip()}\n"


class JudgedQuery(BaseModel):
    """One query and the relevance judgment of every document judged for it, the line the judgments file holds."""

    model_config = ConfigDict(frozen=True)

    query_id: str
    question: str
    judgments: dict[str, int]


def file_stems(documents: Iterable[BeirDocument]) -> dict[str, str]:
    """Each document's cache file name stem, unique even on a filesystem that ignores case.

    A stem is the document id when the id is a plain file name that no other id matches ignoring case, else a
    stable hash of the id.

    Args:
        documents: Every document of the dataset.

    Returns:
        The stem by document id.
    """
    ids = [document.id for document in documents]
    folded = Counter(document_id.casefold() for document_id in ids)
    return {
        document_id: document_id if _SAFE_FILE_STEM.match(document_id) and folded[document_id.casefold()] == 1 else f"id-{_stable_hash(document_id)}"
        for document_id in ids
    }


def _stable_hash(text: str) -> int:
    """A stable integer hash of `text`, the same in every process."""
    value = 0
    for character in text:
        value = (value * 1_000_003 + ord(character)) % (2**61 - 1)
    return value


def parse_documents(lines: Iterable[str]) -> Iterator[BeirDocument]:
    """The documents of `corpus.jsonl`, in file order.

    Args:
        lines: The file's lines.

    Yields:
        Each document.

    Raises:
        BeirDatasetError: When a non-empty line is not a document object.
    """
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            yield BeirDocument(id=str(raw["_id"]), title=str(raw.get("title") or ""), text=str(raw.get("text") or ""))
        except (ValueError, KeyError, TypeError, ValidationError) as error:
            msg = f"corpus.jsonl line {number} is not a document: {error}"
            raise BeirDatasetError(msg) from error


def parse_queries(lines: Iterable[str]) -> dict[str, str]:
    """The question text of every query in `queries.jsonl`, by query id.

    Args:
        lines: The file's lines.

    Returns:
        The questions by id.

    Raises:
        BeirDatasetError: When a non-empty line is not a query object.
    """
    queries: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            queries[str(raw["_id"])] = str(raw["text"])
        except (ValueError, KeyError, TypeError) as error:
            msg = f"queries.jsonl line {number} is not a query: {error}"
            raise BeirDatasetError(msg) from error
    return queries


def parse_judgments(lines: Iterable[str]) -> dict[str, dict[str, int]]:
    """The relevance judgments of a `qrels/<split>.tsv` file, by query id then document id, in file order.

    Args:
        lines: The file's lines, the header first.

    Returns:
        Each query's judged documents and their grades.

    Raises:
        BeirDatasetError: When a non-empty line after the header has no three tab-separated fields or no integer grade.
    """
    judgments: dict[str, dict[str, int]] = {}
    for number, line in enumerate(lines, start=1):
        if number == 1 or not line.strip():
            continue
        fields = line.rstrip("\n").split("\t")
        try:
            query_id, document_id, grade = fields[0], fields[1], int(fields[2])
        except (IndexError, ValueError) as error:
            msg = f"qrels line {number} is not query-id, corpus-id and score: {line.strip()!r}"
            raise BeirDatasetError(msg) from error
        judgments.setdefault(query_id, {})[document_id] = grade
    return judgments


def judged_queries(questions: dict[str, str], judgments: dict[str, dict[str, int]]) -> list[JudgedQuery]:
    """Every judged query that has a question, in the order the judgments file lists them.

    Args:
        questions: The question text by query id.
        judgments: Each query's judged documents and grades.

    Returns:
        One judged query per query id present in both.
    """
    return [
        JudgedQuery(query_id=query_id, question=questions[query_id], judgments=grades)
        for query_id, grades in judgments.items()
        if query_id in questions
    ]
