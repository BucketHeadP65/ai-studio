"""Truth-table specs for the shared `@k` metric-key resolution (`engram.evals.shared.metrics`).

`primary_at_k_key` picks a run's headline cutoff key off its score names: the preferred cutoff when
present, else the smallest numeric cutoff, with non-numeric suffixes sorting last. `at_k_cutoff` is the
sort key that ordering rides on. The diagnosis reader, the recommendation gate, and the over-expansion detector
all resolve their headline recall/precision the same way, so these pin the one shared behavior.
`is_known_metric_key` pins the closed vocabulary a tuning policy's `primary_metric` is validated
against: the unprefixed citation keys and every layer's namespaced keys.
"""

from __future__ import annotations

import pytest

from engram.evals.shared.metrics import PRECISION_PREFIX, RECALL_PREFIX, at_k_cutoff, is_known_metric_key, primary_at_k_key


class TestPrimaryAtKKey:
    def test_prefers_the_preferred_cutoff_when_present(self):
        names = ["recall@1", "recall@5", "recall@10"]
        assert primary_at_k_key(names, prefix=RECALL_PREFIX) == "recall@5"

    def test_falls_back_to_the_smallest_cutoff_when_preferred_absent(self):
        names = ["recall@10", "recall@3", "recall@7"]
        assert primary_at_k_key(names, prefix=RECALL_PREFIX) == "recall@3"

    def test_non_numeric_suffix_sorts_after_every_numeric_cutoff(self):
        names = ["recall@none", "recall@10"]
        assert primary_at_k_key(names, prefix=RECALL_PREFIX) == "recall@10"

    def test_all_non_numeric_suffixes_fall_back_to_the_name_tiebreak(self):
        names = ["recall@none", "recall@all"]
        assert primary_at_k_key(names, prefix=RECALL_PREFIX) == "recall@all"

    def test_no_metric_of_the_family_yields_none(self):
        assert primary_at_k_key(["precision@5", "mrr"], prefix=RECALL_PREFIX) is None

    def test_empty_names_yields_none(self):
        assert primary_at_k_key([], prefix=RECALL_PREFIX) is None

    def test_precision_prefix_resolves_the_precision_family(self):
        names = ["precision@1", "precision@5", "recall@5"]
        assert primary_at_k_key(names, prefix=PRECISION_PREFIX) == "precision@5"

    def test_a_custom_preferred_k_overrides_the_default(self):
        names = ["recall@1", "recall@5", "recall@10"]
        assert primary_at_k_key(names, prefix=RECALL_PREFIX, preferred_k=10) == "recall@10"


class TestAtKCutoff:
    def test_numeric_suffix_yields_its_integer_and_name(self):
        assert at_k_cutoff("recall@5", prefix=RECALL_PREFIX) == (5, "recall@5")

    def test_non_numeric_suffix_sorts_last_via_the_sentinel(self):
        assert at_k_cutoff("recall@none", prefix=RECALL_PREFIX) == (2**31, "recall@none")

    def test_precision_prefix_strips_its_own_prefix(self):
        assert at_k_cutoff("precision@3", prefix=PRECISION_PREFIX) == (3, "precision@3")


class TestIsKnownMetricKey:
    @pytest.mark.parametrize(
        "name",
        [
            "recall@5",
            "precision@1",
            "mrr",
            "citation_recall@5",
            "citation_precision@10",
            "citation_mrr",
            "surfaced_recall@1",
            "surfaced_mrr",
            "ranking_precision@5",
            "ranking_mrr",
        ],
    )
    def test_known_keys_pass(self, name: str):
        assert is_known_metric_key(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "recall",
            "recall@0",
            "recall@-1",
            "recall@abc",
            "total_tokens",
            "n_tool_calls",
            "bogus_recall@5",
            "citation_recall",
        ],
    )
    def test_unknown_keys_fail(self, name: str):
        assert is_known_metric_key(name) is False


def test_ndcg_keys_are_known_metric_keys():
    assert is_known_metric_key("ndcg@10")
    assert is_known_metric_key("surfaced_ndcg@10")
    assert not is_known_metric_key("ndcg@0")
    assert not is_known_metric_key("ndcg@ten")
