"""Unit specs for the retrieval-policy value object (`engram.evals.tuning.policy`).

A `RetrievalPolicy` is the unified controlled variable of the tuning loop: the three retrieval
edit surfaces (the agent instructions, the retrieval strategy, the per-tool MCP description overrides)
bundled into one frozen artifact the loop optimizes and persists. These specs pin the contract: the
strategy is constrained to the eval-valid set, the baseline matches the shipped incumbent, the YAML
round-trip is lossless, and an empty `tool_descriptions` carries the shipped-defaults semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pydantic
import pytest

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.evals.tuning import strategy
from engram.evals.tuning.policy import (
    DEFAULT_INSTRUCTIONS,
    RetrievalPolicy,
    baseline_policy,
    effective_baseline_policy,
    load_active_policy,
    load_policy,
    save_policy,
)

if TYPE_CHECKING:
    from pathlib import Path


class RetrievalPolicyFactory(ModelFactory[RetrievalPolicy]):
    """A `RetrievalPolicy` with an in-set strategy (polyfactory's random string would fail the validator)."""

    strategy = strategy.DEFAULT_STRATEGY.value


class TestRetrievalPolicy:
    def test_strategy_accepts_every_eval_valid_name(self):
        for name in strategy.EVAL_STRATEGY_NAMES:
            policy = RetrievalPolicy(instructions="x", strategy=name)

            assert policy.strategy == name

    def test_an_unknown_strategy_is_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            RetrievalPolicy(instructions="x", strategy="nonsense")

    def test_code_mode_is_rejected_as_an_eval_axis(self):
        # code-mode is excluded from EVAL_STRATEGY_NAMES by design (a no-op eval axis), so it must
        # be refused on a policy even though it stays a valid MCP retrieval_profile.
        assert strategy.RetrievalStrategy.CODE_MODE.value not in strategy.EVAL_STRATEGY_NAMES
        with pytest.raises(pydantic.ValidationError):
            RetrievalPolicy(instructions="x", strategy=strategy.RetrievalStrategy.CODE_MODE.value)

    def test_tool_descriptions_default_to_empty(self):
        # Empty means "use the shipped serve/tools.py defaults verbatim" (the override seam).
        policy = RetrievalPolicy(instructions="x", strategy=strategy.DEFAULT_STRATEGY.value)

        assert policy.tool_descriptions == {}

    def test_tool_descriptions_carry_per_tool_overrides(self):
        overrides = {"search": "Find supporting sources.", "fetch": "Read one hit's body."}
        policy = RetrievalPolicy(instructions="x", strategy=strategy.DEFAULT_STRATEGY.value, tool_descriptions=overrides)

        assert policy.tool_descriptions == overrides

    def test_the_policy_is_frozen(self):
        policy = RetrievalPolicy(instructions="x", strategy=strategy.DEFAULT_STRATEGY.value)

        with pytest.raises(pydantic.ValidationError):
            policy.instructions = "mutated"


class TestBaselinePolicy:
    def test_baseline_is_the_shipped_incumbent(self):
        policy = baseline_policy()

        assert policy.strategy == "full"
        assert policy.strategy == strategy.DEFAULT_STRATEGY.value
        assert policy.instructions == DEFAULT_INSTRUCTIONS
        assert policy.tool_descriptions == {}


class TestPolicyRoundTrip:
    def test_save_then_load_is_identity(self, tmp_path: Path):
        policy = RetrievalPolicy(
            instructions="Answer the question and cite the page ids.",
            strategy="search-hybrid",
            tool_descriptions={"search": "Hybrid search over the knowledge base."},
        )
        path = tmp_path / "policy.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy

    def test_round_trip_preserves_the_baseline(self, tmp_path: Path):
        path = tmp_path / "baseline.yaml"

        save_policy(baseline_policy(), path)

        assert load_policy(path) == baseline_policy()

    def test_a_factory_built_policy_round_trips(self, tmp_path: Path):
        policy = RetrievalPolicyFactory.build()
        path = tmp_path / "factory.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy


class TestDefaultInstructions:
    def test_default_instructions_is_a_sectioned_grounded_budgeted_contract(self):
        # The shipped default is a sectioned contract (role / workflow / grounding / retrieval budget /
        # citations). Asserting on its load-bearing sections rather than the whole literal keeps this
        # robust to wording tweaks while still locking in the discipline that matters.
        assert "# Role" in DEFAULT_INSTRUCTIONS
        assert "# Workflow" in DEFAULT_INSTRUCTIONS
        assert "Answer only from the retrieved content" in DEFAULT_INSTRUCTIONS  # grounding / anti-hallucination
        assert "# Retrieval budget" in DEFAULT_INSTRUCTIONS  # the stop conditions that bound runaway retrieval
        assert "Never repeat a search you have already run" in DEFAULT_INSTRUCTIONS
        assert "Cite the sources you actually used" in DEFAULT_INSTRUCTIONS  # the citation contract

    def test_retrieval_budget_steers_snippet_first_fetching(self):
        # Measured from Logfire: runs fetched 17-72 distinct sources (one 72-fetch run rate-limited
        # itself and produced NO answer). The budget must steer the agent to lean on the hit snippets
        # and fetch full text only for the few it will cite — the highest-signal lever on breadth.
        # Kept domain-neutral (snippet/hit are retrieval vocabulary).
        lowered = DEFAULT_INSTRUCTIONS.lower()
        assert "snippet" in lowered  # the search-hit snippet the budget tells it to read first
        assert "every hit" in lowered  # ...only for the few it will cite, "not every hit"

    def test_retrieval_budget_has_a_firm_stop_after_a_handful_of_fetches(self):
        # The snippet-first steer alone was too weak to curb the runaway (a 72-fetch run). The budget
        # must now firmly bound fetch breadth: fetch only a handful, then stop and answer from what you
        # have (a sharper search, not more fetching, is the fix). "a handful" is principled guidance,
        # not an arbitrary hard cap. Kept domain-neutral.
        lowered = DEFAULT_INSTRUCTIONS.lower()
        assert "handful" in lowered  # the natural-language bound on how many sources to fetch
        assert "stop and answer" in lowered  # the firm stop condition once a handful is fetched

    def test_retrieval_budget_steers_parallel_batched_fetching(self):
        # Measured: the model fetched one doc per turn (35 fetches over ~36 requests) — slow, many
        # round-trips, rate-limit-prone. pydantic-ai runs parallel tool calls, so the budget tells it
        # to issue several fetches together in ONE step (they run in parallel) instead of one at a
        # time. True and realistic — the existing single-id fetch called multiple times in one turn.
        lowered = DEFAULT_INSTRUCTIONS.lower()
        assert "in parallel" in lowered  # names the true parallel execution the batching exploits
        assert "single step" in lowered or "one step" in lowered  # issue the fetches together, not one per turn

    def test_default_instructions_is_domain_general(self):
        # engram indexes ANY knowledge base, so the shipped default must not leak the research domain.
        lowered = DEFAULT_INSTRUCTIONS.lower()
        assert "research assistant" not in lowered
        assert "knowledge memory" not in lowered
        assert "papers" not in lowered


class TestLoadActivePolicy:
    """`load_active_policy` is the typed serve-read tail of the tuning loop (F35).

    It owns the `<knowledge_base>/_meta/active-policy.yaml` convention and resolves the policy's strategy
    against the LIVE serve strategy set at load (`strategy.resolve_strategy` / `STRATEGY_NAMES`), so a
    stale policy whose strategy the serve surface no longer supports fails HERE, at the accessor
    boundary, not deep in MCP startup. An absent file leaves the serve surface as the knowledge_base_schema declared.
    """

    def test_absent_file_returns_none(self, tmp_path: Path):
        # No `_meta/active-policy.yaml` → the serve surface stays exactly as the knowledge_base_schema declared it.
        assert load_active_policy(tmp_path) is None

    def test_valid_policy_resolves_against_the_live_serve_set(self, tmp_path: Path):
        policy = RetrievalPolicy(
            instructions="Answer the question and cite the page ids.",
            strategy="search-keyword",
            tool_descriptions={"search": "Keyword search over the knowledge base."},
        )
        (tmp_path / "_meta").mkdir()
        save_policy(policy, tmp_path / "_meta" / "active-policy.yaml")

        resolution = load_active_policy(tmp_path)

        assert resolution is not None
        assert resolution.profile == strategy.resolve_strategy("search-keyword")
        assert resolution.tool_descriptions == {"search": "Keyword search over the knowledge base."}

    def test_resolution_exposes_the_policy_instructions(self, tmp_path: Path):
        # The active-policy file stores the full policy, so the serve-read resolution carries the
        # tuned instructions verbatim (the per-knowledge-base instructions override).
        policy = RetrievalPolicy(instructions="Answer the question and cite the page ids.", strategy="search-keyword")
        (tmp_path / "_meta").mkdir()
        save_policy(policy, tmp_path / "_meta" / "active-policy.yaml")

        resolution = load_active_policy(tmp_path)

        assert resolution is not None
        assert resolution.instructions == "Answer the question and cite the page ids."

    def test_empty_tool_descriptions_round_trip(self, tmp_path: Path):
        # An empty mapping means "use the shipped serve/tools.py descriptions verbatim".
        (tmp_path / "_meta").mkdir()
        save_policy(baseline_policy(), tmp_path / "_meta" / "active-policy.yaml")

        resolution = load_active_policy(tmp_path)

        assert resolution is not None
        assert resolution.profile == strategy.resolve_strategy(strategy.DEFAULT_STRATEGY.value)
        assert resolution.tool_descriptions == {}

    def test_serve_unsupported_strategy_fails_at_the_accessor_boundary(self, tmp_path: Path):
        # A stale policy artifact carrying a strategy the live serve surface no longer supports must
        # fail HERE (at load), not deferred to MCP startup. `RetrievalPolicy`'s validator rejects any
        # name outside the eval-valid set, so a removed/unknown strategy never reaches resolution.
        (tmp_path / "_meta").mkdir()
        (tmp_path / "_meta" / "active-policy.yaml").write_text("strategy: ancient-removed-strategy\ninstructions: i\n", encoding="utf-8")

        with pytest.raises((pydantic.ValidationError, ValueError)):
            load_active_policy(tmp_path)


class TestEffectiveBaselinePolicy:
    """`effective_baseline_policy` is the knowledge-base-aware incumbent, the tuning loop's starting point.

    It returns the knowledge base's active `RetrievalPolicy` when `_meta/active-policy.yaml` is present so a
    tuning round improves on the last tuned policy rather than the shipped default, and falls
    back to `baseline_policy()` on a fresh knowledge_base. A malformed file raises the same clean error as the
    `load_active_policy` accessor rather than silently falling back to the shipped baseline.
    """

    def test_absent_file_falls_back_to_the_shipped_baseline(self, tmp_path: Path):
        # A fresh knowledge base with no active policy → the shipped incumbent, byte-identical to baseline_policy().
        assert effective_baseline_policy(tmp_path) == baseline_policy()

    def test_present_file_returns_the_knowledge_bases_policy(self, tmp_path: Path):
        policy = RetrievalPolicy(
            instructions="Answer the question and cite the page ids.",
            strategy="search-hybrid",
            tool_descriptions={"search": "Hybrid search over the knowledge base."},
        )
        (tmp_path / "_meta").mkdir()
        save_policy(policy, tmp_path / "_meta" / "active-policy.yaml")

        assert effective_baseline_policy(tmp_path) == policy

    def test_malformed_file_raises_the_same_clean_error(self, tmp_path: Path):
        # A tampered artifact must fail at the accessor boundary (never silently fall back to baseline),
        # the same contract load_active_policy raises: a ValidationError for a bad shape / out-of-set
        # strategy, or a ValueError for a strategy the live serve set no longer supports.
        (tmp_path / "_meta").mkdir()
        (tmp_path / "_meta" / "active-policy.yaml").write_text("strategy: ancient-removed-strategy\ninstructions: i\n", encoding="utf-8")

        with pytest.raises((pydantic.ValidationError, ValueError)):
            effective_baseline_policy(tmp_path)
