"""Unit tests for the retrieval-policy value object (`core.ai.retrieval.policy`).

A `RetrievalPolicy` is the unified controlled variable of a tuning loop: the agent instructions,
the retrieval strategy, and the per-tool description overrides bundled into one frozen artifact. These
specs pin the contract: the strategy is constrained to the tuning-valid set, the baseline carries
the given instructions, the YAML round-trip is lossless, and the path-parameterized accessors resolve
against the live strategy set (failing at load on a stale or malformed artifact).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pydantic
import pytest

from polyfactory.factories.pydantic_factory import ModelFactory

from core.ai.retrieval import strategy
from core.ai.retrieval.policy import (
    ActivePolicyResolution,
    RetrievalPolicy,
    baseline_policy,
    effective_baseline_policy,
    load_active_policy,
    load_policy,
    save_policy,
)

if TYPE_CHECKING:
    from pathlib import Path

_INSTRUCTIONS = "Answer the question and cite the sources you used."


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

    def test_code_mode_is_rejected_as_a_tuning_axis(self):
        assert strategy.RetrievalStrategy.CODE_MODE.value not in strategy.EVAL_STRATEGY_NAMES
        with pytest.raises(pydantic.ValidationError):
            RetrievalPolicy(instructions="x", strategy=strategy.RetrievalStrategy.CODE_MODE.value)

    def test_tool_descriptions_default_to_empty(self):
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
    def test_baseline_carries_the_given_instructions_and_the_default_strategy(self):
        policy = baseline_policy(_INSTRUCTIONS)

        assert policy.strategy == "full"
        assert policy.strategy == strategy.DEFAULT_STRATEGY.value
        assert policy.instructions == _INSTRUCTIONS
        assert policy.tool_descriptions == {}

    def test_baseline_accepts_an_explicit_strategy(self):
        policy = baseline_policy(_INSTRUCTIONS, strategy_name="search-hybrid")

        assert policy.strategy == "search-hybrid"


class TestPolicyRoundTrip:
    def test_save_then_load_is_identity(self, tmp_path: Path):
        policy = RetrievalPolicy(
            instructions=_INSTRUCTIONS, strategy="search-hybrid", tool_descriptions={"search": "Hybrid search over the knowledge base."}
        )
        path = tmp_path / "policy.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy

    def test_saving_into_a_directory_that_does_not_exist_creates_it(self, tmp_path: Path):
        policy = RetrievalPolicy(instructions=_INSTRUCTIONS, strategy="search-hybrid")
        path = tmp_path / "evals" / "tuning" / "research.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy

    def test_round_trip_preserves_the_baseline(self, tmp_path: Path):
        path = tmp_path / "baseline.yaml"

        save_policy(baseline_policy(_INSTRUCTIONS), path)

        assert load_policy(path) == baseline_policy(_INSTRUCTIONS)

    def test_a_factory_built_policy_round_trips(self, tmp_path: Path):
        policy = RetrievalPolicyFactory.build()
        path = tmp_path / "factory.yaml"

        save_policy(policy, path)

        assert load_policy(path) == policy


class TestLoadActivePolicy:
    """`load_active_policy(path)` resolves a persisted policy against the live strategy set at load."""

    def test_absent_file_returns_none(self, tmp_path: Path):
        assert load_active_policy(tmp_path / "active-policy.yaml") is None

    def test_valid_policy_resolves_against_the_live_set(self, tmp_path: Path):
        policy = RetrievalPolicy(
            instructions=_INSTRUCTIONS, strategy="search-keyword", tool_descriptions={"search": "Keyword search over the knowledge base."}
        )
        path = tmp_path / "active-policy.yaml"
        save_policy(policy, path)

        resolution = load_active_policy(path)

        assert isinstance(resolution, ActivePolicyResolution)
        assert resolution.profile == strategy.resolve_strategy("search-keyword")
        assert resolution.tool_descriptions == {"search": "Keyword search over the knowledge base."}
        assert resolution.instructions == _INSTRUCTIONS

    def test_unsupported_strategy_fails_at_the_accessor_boundary(self, tmp_path: Path):
        path = tmp_path / "active-policy.yaml"
        path.write_text("strategy: ancient-removed-strategy\ninstructions: i\n", encoding="utf-8")

        with pytest.raises((pydantic.ValidationError, ValueError)):
            load_active_policy(path)


class TestEffectiveBaselinePolicy:
    """`effective_baseline_policy(path, default_instructions=...)` is the consumer-aware incumbent."""

    def test_absent_file_falls_back_to_the_shipped_baseline(self, tmp_path: Path):
        assert effective_baseline_policy(tmp_path / "active-policy.yaml", default_instructions=_INSTRUCTIONS) == baseline_policy(
            _INSTRUCTIONS
        )

    def test_present_file_returns_the_persisted_policy(self, tmp_path: Path):
        policy = RetrievalPolicy(instructions=_INSTRUCTIONS, strategy="search-hybrid")
        path = tmp_path / "active-policy.yaml"
        save_policy(policy, path)

        assert effective_baseline_policy(path, default_instructions="unused") == policy

    def test_malformed_file_raises_the_same_clean_error(self, tmp_path: Path):
        path = tmp_path / "active-policy.yaml"
        path.write_text("strategy: ancient-removed-strategy\ninstructions: i\n", encoding="utf-8")

        with pytest.raises((pydantic.ValidationError, ValueError)):
            effective_baseline_policy(path, default_instructions=_INSTRUCTIONS)
