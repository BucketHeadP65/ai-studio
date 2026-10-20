"""Unit tests for `source_identities`, the identifiers that name one source whatever connector brought it."""

from __future__ import annotations

from engram.sources.domain.source_queue import source_identities


def test_an_arxiv_url_names_its_url_and_its_arxiv_id_without_the_version() -> None:
    assert source_identities(url="https://arxiv.org/pdf/2401.00001v3", doi=None) == {
        "url:https://arxiv.org/pdf/2401.00001v3",
        "arxiv:2401.00001",
    }


def test_a_doi_names_itself_in_lower_case() -> None:
    assert source_identities(url=None, doi=" 10.1000/Example.42 ") == {"doi:10.1000/example.42"}


def test_a_doi_url_names_the_doi_it_carries() -> None:
    assert "doi:10.1000/example.42" in source_identities(url="https://doi.org/10.1000/Example.42", doi=None)


def test_nothing_names_nothing() -> None:
    assert source_identities(url=None, doi=None) == frozenset()
    assert source_identities(url="  ", doi="") == frozenset()


def test_an_arxiv_doi_names_its_arxiv_id() -> None:
    assert "arxiv:2401.00001" in source_identities(url="https://publisher.example/paper", doi="10.48550/arXiv.2401.00001")


def test_an_arxiv_doi_url_names_its_arxiv_id() -> None:
    assert "arxiv:2401.00001" in source_identities(url="https://doi.org/10.48550/arXiv.2401.00001", doi=None)
