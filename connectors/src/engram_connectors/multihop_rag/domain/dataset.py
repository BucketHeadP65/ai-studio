"""The MultiHop-RAG dataset's two files as values: its news articles and its questions with their evidence.

`corpus.json` holds one JSON array of article objects with the keys `title`, `author`, `source`, `published_at`,
`category`, `url` and `body`. `MultiHopRAG.json` holds one JSON array of question objects with the keys `query`,
`answer`, `question_type` and `evidence_list`, each evidence naming the title of the article it comes from.
"""

from __future__ import annotations

import hashlib

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from engram_connectors.multihop_rag.exceptions import MultihopRagDatasetError

ARTICLES_FILE = "corpus.json"
"""The dataset file that holds the articles."""

QUESTIONS_FILE = "MultiHopRAG.json"
"""The dataset file that holds the questions."""

QueryType = Literal["inference_query", "comparison_query", "temporal_query", "null_query"]
"""The query types the dataset labels its questions with."""


class Article(BaseModel):
    """One news article of the dataset."""

    model_config = ConfigDict(frozen=True)

    title: str
    author: str | None = None
    """The author with surrounding space removed, `None` when the dataset gives none or a blank one."""
    outlet: str = Field(validation_alias="source")
    """The publication the article appeared in, the dataset's `source` key."""
    published_at: str
    """When the article was published, ISO 8601 date and time with its offset, verbatim."""
    category: str
    url: str
    body: str

    @field_validator("author")
    @classmethod
    def _present_author(cls, value: str | None) -> str | None:
        """Turn a blank author into `None`."""
        return (value or "").strip() or None

    @field_validator("published_at")
    @classmethod
    def _readable_time(cls, value: str) -> str:
        """Refuse a published time that is not ISO 8601."""
        datetime.fromisoformat(value)
        return value

    @property
    def source_id(self) -> str:
        """`mhr-` and the first 12 hex characters of the SHA-256 of the url, stable across downloads."""
        return "mhr-" + hashlib.sha256(self.url.encode()).hexdigest()[:12]

    @property
    def published(self) -> date:
        """The calendar date of the published time, in the offset the dataset states."""
        return datetime.fromisoformat(self.published_at).date()

    def markdown(self) -> str:
        """The mirror text: the title as a heading, a blank line, then the body.

        Returns:
            The markdown text.
        """
        return f"# {self.title.strip()}\n\n{self.body.strip()}\n"


class Evidence(BaseModel):
    """One piece of evidence a question cites, named by the title of the article it comes from."""

    model_config = ConfigDict(frozen=True)

    title: str


class Question(BaseModel):
    """One question of the dataset with its gold answer, its query type and its evidence."""

    model_config = ConfigDict(frozen=True)

    question: str = Field(validation_alias="query")
    answer: str
    query_type: QueryType = Field(validation_alias="question_type")
    evidence: list[Evidence] = Field(validation_alias="evidence_list")
    """Empty for a question the articles cannot answer."""


class JudgedQuery(BaseModel):
    """One question and the source id of every article its evidence comes from, the line the judgments file holds."""

    model_config = ConfigDict(frozen=True)

    query_id: str
    question: str
    judgments: dict[str, int]
    answer: str
    query_type: QueryType


class DatasetHashes(BaseModel):
    """The SHA-256 prefix each downloaded dataset file must start with."""

    model_config = ConfigDict(frozen=True)

    articles: str = Field(pattern=r"^[0-9a-f]{1,64}$")
    """The prefix of the articles file's hash."""
    questions: str = Field(pattern=r"^[0-9a-f]{1,64}$")
    """The prefix of the questions file's hash."""


_ARTICLES = TypeAdapter(list[Article])
_QUESTIONS = TypeAdapter(list[Question])


def parse_articles(data: bytes) -> list[Article]:
    """The articles of the articles file, in file order.

    Args:
        data: The file's bytes.

    Returns:
        Every article.

    Raises:
        MultihopRagDatasetError: When the file is not a JSON array of articles, or two articles share a source id or a title.
    """
    try:
        articles = _ARTICLES.validate_json(data)
    except ValidationError as error:
        msg = f"{ARTICLES_FILE} is not a list of articles: {error}"
        raise MultihopRagDatasetError(msg) from error
    urls_by_id: defaultdict[str, list[str]] = defaultdict(list)
    for article in articles:
        urls_by_id[article.source_id].append(article.url)
    for source_id, urls in urls_by_id.items():
        if len(urls) > 1:
            msg = f"{ARTICLES_FILE} holds more than one article whose url gives the source id {source_id}: {', '.join(urls)}"
            raise MultihopRagDatasetError(msg)
    for title, count in Counter(article.title for article in articles).items():
        if count > 1:
            msg = f"{ARTICLES_FILE} holds more than one article with the title {title!r}"
            raise MultihopRagDatasetError(msg)
    return articles


def parse_questions(data: bytes) -> list[Question]:
    """The questions of the questions file, in file order.

    Args:
        data: The file's bytes.

    Returns:
        Every question.

    Raises:
        MultihopRagDatasetError: When the file is not a JSON array of questions.
    """
    try:
        return _QUESTIONS.validate_json(data)
    except ValidationError as error:
        msg = f"{QUESTIONS_FILE} is not a list of questions: {error}"
        raise MultihopRagDatasetError(msg) from error


def judged_queries(articles: list[Article], questions: list[Question]) -> list[JudgedQuery]:
    """Every question as a judged query, numbered in file order from `q0001`.

    Each article an evidence names by its exact title is judged relevant with grade 1. A question with no
    evidence keeps empty judgments.

    Args:
        articles: Every article of the dataset.
        questions: Every question, in file order.

    Returns:
        One judged query per question.

    Raises:
        MultihopRagDatasetError: When an evidence names a title no article holds.
    """
    ids = {article.title: article.source_id for article in articles}
    queries: list[JudgedQuery] = []
    for number, question in enumerate(questions, start=1):
        query_id = f"q{number:04d}"
        judgments: dict[str, int] = {}
        for evidence in question.evidence:
            if evidence.title not in ids:
                msg = f"question {query_id} cites the article {evidence.title!r}, which {ARTICLES_FILE} does not hold"
                raise MultihopRagDatasetError(msg)
            judgments[ids[evidence.title]] = 1
        queries.append(
            JudgedQuery(
                query_id=query_id,
                question=question.question,
                judgments=judgments,
                answer=question.answer,
                query_type=question.query_type,
            ),
        )
    return queries
