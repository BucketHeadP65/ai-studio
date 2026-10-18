from dataclasses import dataclass

from engram.sources.contracts import SourceType, arxiv_id_of, claimants_for, classify_url, doi_of
from engram.testing.fakes.connector import FakeConnector


@dataclass
class _FakeClaimant:
    """A minimal `UrlClaimant`: claims every URL as `source_type` (or none when it is `None`), keyed by `key`."""

    source_type: SourceType | None
    key: str = "fake-key"

    def claims(self, url: str) -> SourceType | None:
        del url
        return self.source_type

    def key_for(self, url: str) -> str:
        del url
        return self.key


def test_arxiv_id_of_reads_abs_pdf_and_html_urls_without_the_version() -> None:
    assert arxiv_id_of("https://arxiv.org/abs/2505.10978") == "2505.10978"
    assert arxiv_id_of("https://www.arxiv.org/pdf/2505.10978v2") == "2505.10978"
    assert arxiv_id_of("https://arxiv.org/html/2505.10978v1") == "2505.10978"
    assert arxiv_id_of("https://github.com/langfengQ/verl-agent") is None


def test_doi_of_reads_doi_org_urls_only() -> None:
    assert doi_of("https://doi.org/10.1000/xyz123") == "10.1000/xyz123"
    assert doi_of("http://dx.doi.org/10.1000/xyz123") == "10.1000/xyz123"
    assert doi_of("https://doi.org/") is None
    assert doi_of("https://arxiv.org/abs/2505.10978") is None


class TestProviderDrivenClassification:
    """`classify_url(url, connectors=...)` asks each claimant in order; no host is matched outside one.

    Real connector recognition (a pasted YouTube/arXiv/X URL classified by the real, installed
    connectors) is `connectors`' own concern, covered there — this class pins only `classify.py`'s
    dispatch logic, against local fakes, so it never needs a connector package installed.
    """

    def test_the_first_provider_to_claim_a_url_decides_its_kind(self) -> None:
        source_type, _key = classify_url("https://example.org/x", connectors=[_FakeClaimant(SourceType.VIDEO)])

        assert source_type is SourceType.VIDEO

    def test_a_provider_that_does_not_claim_the_url_yields_to_the_next(self) -> None:
        source_type, _key = classify_url(
            "https://example.org/x", connectors=[_FakeClaimant(None), _FakeClaimant(SourceType.POST)]
        )

        assert source_type is SourceType.POST

    def test_provider_order_decides_which_kind_wins_on_overlap(self) -> None:
        first, _ = classify_url(
            "https://example.org/x", connectors=[_FakeClaimant(SourceType.ARTICLE), _FakeClaimant(SourceType.VIDEO)]
        )
        second, _ = classify_url(
            "https://example.org/x", connectors=[_FakeClaimant(SourceType.VIDEO), _FakeClaimant(SourceType.ARTICLE)]
        )

        assert first is SourceType.ARTICLE
        assert second is SourceType.VIDEO

    def test_no_provider_claiming_the_url_falls_back_to_article(self) -> None:
        source_type, key = classify_url("https://example.org/x", connectors=())

        assert source_type is SourceType.ARTICLE
        assert key.startswith("url-")

    def test_the_fallback_key_is_stable_and_sixteen_hex_characters(self) -> None:
        first = classify_url("https://blog.example.org/posts/agents?utm=1", connectors=())
        second = classify_url("https://blog.example.org/posts/agents?utm=1", connectors=())

        assert first == second
        assert len(first[1]) == len("url-") + 16

    def test_the_key_comes_from_whichever_claimant_claimed_the_url(self) -> None:
        _source_type, key = classify_url("https://example.org/x", connectors=[_FakeClaimant(SourceType.VIDEO, key="video-0001")])

        assert key == "video-0001"


class TestClaimantsFor:
    """`claimants_for` — the knowledge base's declared connectors, in order, resolved through `engram.sources.registry`.

    Resolving a real name to its real, installed class (and that class's own `claims`/`key_for`
    correctly recognizing its URLs) is `engram.sources.registry`'s own contract, covered by
    `test_registry.py` against a fake, plus every test in the `connectors` project's own suite that
    constructs a connector through `claimants_for` at all. This class covers only
    what belongs to `classify.py` itself: skip-on-unresolved, the empty-input edge, and threading
    an injected `source` through to `connector_class`.
    """

    def test_an_unrecognized_or_uninstalled_name_is_skipped_not_raised(self) -> None:
        assert claimants_for(("vimeo",)) == ()

    def test_empty_names_resolves_to_no_claimants(self) -> None:
        assert claimants_for(()) == ()

    def test_an_injected_source_resolves_a_name_with_no_real_package_installed(self) -> None:
        fake = FakeConnector()
        resolved = claimants_for(("fake",), source={"fake": fake})

        assert resolved == (fake,)

    def test_an_injected_source_still_skips_a_name_it_does_not_carry(self) -> None:
        fake = FakeConnector()
        assert claimants_for(("fake", "vimeo"), source={"fake": fake}) == (fake,)
