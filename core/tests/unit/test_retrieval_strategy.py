"""Unit tests for the retrieval-strategy registry (`core.ai.retrieval.strategy`).

The registry maps a named strategy to a `RetrievalProfile` of primitives (tool subset plus forced
search leg) that a toolset builder consumes. These specs pin the contract: every offered name
resolves, the default is the full suite, the search and graph families compose as documented, and an
unknown name is rejected rather than silently defaulted.
"""

from __future__ import annotations

import pytest

from core.ai.retrieval.names import FETCH, FIND_PATHS, GRAPH_EXPAND, RUN_CODE, SEARCH, TOOL_NAMES
from core.ai.retrieval.strategy import DEFAULT_STRATEGY, EVAL_STRATEGY_NAMES, STRATEGY_NAMES, RetrievalStrategy, resolve_strategy

# The canonical retrieval tools plus the `run_code` code-mode meta-tool: a strategy may additionally
# expose `run_code` (the `code-mode` strategy does), which is deliberately not in `TOOL_NAMES`.
_OFFERABLE_TOOLS = TOOL_NAMES | {RUN_CODE}


class TestStrategyNames:
    def test_every_offered_name_resolves_to_a_profile(self):
        for name in STRATEGY_NAMES:
            profile = resolve_strategy(name)
            assert profile.tools <= _OFFERABLE_TOOLS  # never names a tool outside the retrieval set + run_code
            assert profile.tools  # every strategy exposes at least one tool

    def test_the_default_is_listed_first(self):
        assert STRATEGY_NAMES[0] == DEFAULT_STRATEGY.value

    def test_names_match_the_enum(self):
        assert set(STRATEGY_NAMES) == {s.value for s in RetrievalStrategy}


class TestEvalStrategyNames:
    """`EVAL_STRATEGY_NAMES` (the tuning axis) is the registry minus the code-mode-only profile."""

    def test_eval_names_are_the_full_set_minus_code_mode(self):
        assert set(EVAL_STRATEGY_NAMES) == set(STRATEGY_NAMES) - {RetrievalStrategy.CODE_MODE.value}

    def test_code_mode_is_not_offered_as_a_tuning_strategy(self):
        assert RetrievalStrategy.CODE_MODE.value not in EVAL_STRATEGY_NAMES

    def test_eval_names_preserve_the_default_first_order(self):
        assert EVAL_STRATEGY_NAMES[0] == DEFAULT_STRATEGY.value
        assert tuple(n for n in STRATEGY_NAMES if n != RetrievalStrategy.CODE_MODE.value) == EVAL_STRATEGY_NAMES

    def test_code_mode_remains_a_resolvable_profile(self):
        profile = resolve_strategy(RetrievalStrategy.CODE_MODE.value)

        assert RUN_CODE in profile.tools


class TestResolveStrategy:
    def test_none_is_the_default_full_suite(self):
        profile = resolve_strategy(None)

        assert profile.tools == TOOL_NAMES  # all four tools
        assert profile.search_hybrid is True  # the full suite forces hybrid search

    def test_search_hybrid_is_the_search_pair_in_hybrid_mode(self):
        profile = resolve_strategy("search-hybrid")

        assert profile.tools == frozenset({SEARCH, FETCH})
        assert profile.search_hybrid is True

    def test_search_keyword_is_the_search_pair_in_bm25_mode(self):
        profile = resolve_strategy("search-keyword")

        assert profile.tools == frozenset({SEARCH, FETCH})
        assert profile.search_hybrid is False

    def test_graph_only_is_the_graph_pair_plus_fetch_with_no_search_mode(self):
        profile = resolve_strategy("graph-only")

        assert profile.tools == frozenset({GRAPH_EXPAND, FIND_PATHS, FETCH})  # a traversal an agent can also read
        assert profile.search_hybrid is None  # no search tool means the search mode is irrelevant

    def test_keyword_graph_is_all_tools_in_bm25_mode(self):
        profile = resolve_strategy("keyword-graph")

        assert profile.tools == TOOL_NAMES
        assert profile.search_hybrid is False

    def test_code_mode_is_the_full_hybrid_suite_plus_run_code(self):
        profile = resolve_strategy("code-mode")

        assert profile.tools == TOOL_NAMES | {RUN_CODE}
        assert profile.search_hybrid is True

    def test_run_code_is_opt_in_only_the_code_mode_strategy_exposes_it(self):
        with_run_code = {name for name in STRATEGY_NAMES if RUN_CODE in resolve_strategy(name).tools}

        assert with_run_code == {RetrievalStrategy.CODE_MODE.value}

    def test_an_unknown_strategy_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown retrieval strategy"):
            resolve_strategy("bogus")
