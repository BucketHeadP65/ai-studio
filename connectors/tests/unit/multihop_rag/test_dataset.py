"""The MultiHop-RAG files as values: articles keyed by their url, and questions judged against those articles."""

from __future__ import annotations

import hashlib
import json

from datetime import date
from typing import TYPE_CHECKING

import pytest

from engram_connectors.multihop_rag.contracts import MultihopRagDatasetError
from engram_connectors.multihop_rag.domain.dataset import judged_queries, parse_articles, parse_questions

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue


def _article(title: str, url: str, *, author: str | None = "Ada Writer") -> dict[str, str | None]:
    return {
        "title": title,
        "author": author,
        "source": "Example Daily",
        "published_at": "2023-10-02T09:30:00+00:00",
        "category": "technology",
        "url": url,
        "body": f"{title} body.",
    }


def _question(query: str, question_type: str, titles: list[str]) -> dict[str, JsonValue]:
    evidence: list[JsonValue] = [
        {"title": title, "author": None, "url": "", "source": "", "category": "", "published_at": "", "fact": "A fact."} for title in titles
    ]
    return {"query": query, "answer": "Yes", "question_type": question_type, "evidence_list": evidence}


def _bytes(value: Sequence[Mapping[str, JsonValue]]) -> bytes:
    return json.dumps(value).encode()


def test_an_article_is_keyed_by_the_hash_of_its_url() -> None:
    [article] = parse_articles(_bytes([_article("Alpha", "https://example.test/alpha")]))

    assert article.source_id == "mhr-" + hashlib.sha256(b"https://example.test/alpha").hexdigest()[:12]
    assert article.outlet == "Example Daily"
    assert article.published == date(2023, 10, 2)


def test_two_articles_with_the_same_url_are_refused_naming_the_url() -> None:
    data = _bytes([_article("Alpha", "https://example.test/same"), _article("Beta", "https://example.test/same")])

    with pytest.raises(MultihopRagDatasetError, match=r"https://example\.test/same"):
        parse_articles(data)


def test_two_articles_with_the_same_title_are_refused_naming_the_title() -> None:
    data = _bytes([_article("Same headline", "https://example.test/one"), _article("Same headline", "https://example.test/two")])

    with pytest.raises(MultihopRagDatasetError, match="Same headline"):
        parse_articles(data)


def test_a_file_that_is_not_a_list_of_articles_is_refused_naming_it() -> None:
    with pytest.raises(MultihopRagDatasetError, match=r"corpus\.json"):
        parse_articles(_bytes([{"title": "No url"}]))


def test_an_unreadable_published_time_is_refused() -> None:
    article = _article("Alpha", "https://example.test/alpha") | {"published_at": "last Tuesday"}

    with pytest.raises(MultihopRagDatasetError, match=r"corpus\.json"):
        parse_articles(_bytes([article]))


def test_an_unknown_question_type_is_refused_naming_the_file() -> None:
    with pytest.raises(MultihopRagDatasetError, match=r"MultiHopRAG\.json"):
        parse_questions(_bytes([_question("Why?", "opinion_query", [])]))


def test_questions_are_numbered_in_file_order_and_judged_against_their_evidence() -> None:
    articles = parse_articles(_bytes([_article("Alpha", "https://example.test/alpha"), _article("Beta", "https://example.test/beta")]))
    questions = parse_questions(
        _bytes([_question("Both?", "comparison_query", ["Alpha", "Beta", "Alpha"]), _question("Nothing?", "null_query", [])]),
    )

    queries = judged_queries(articles, questions)

    alpha, beta = articles
    assert [query.query_id for query in queries] == ["q0001", "q0002"]
    assert queries[0].judgments == {alpha.source_id: 1, beta.source_id: 1}
    assert queries[0].query_type == "comparison_query"
    assert queries[1].judgments == {}
    assert queries[1].answer == "Yes"


def test_an_evidence_title_missing_from_the_articles_is_refused_naming_it() -> None:
    articles = parse_articles(_bytes([_article("Alpha", "https://example.test/alpha")]))
    questions = parse_questions(_bytes([_question("Where?", "inference_query", ["Ghost headline"])]))

    with pytest.raises(MultihopRagDatasetError, match="Ghost headline"):
        judged_queries(articles, questions)


def test_an_article_s_markdown_is_its_title_as_a_heading_then_its_body() -> None:
    raw = _article("  Alpha  ", "https://example.test/alpha") | {"body": "First line.\n\nSecond line.\n\n"}
    [article] = parse_articles(_bytes([raw]))

    assert article.markdown() == "# Alpha\n\nFirst line.\n\nSecond line.\n"
