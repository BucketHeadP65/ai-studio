"""The retrieval policy: the one value a tuning loop tunes and saves.

A policy holds the agent-under-test system instructions, the retrieval strategy (which tools and
search leg the agent gets) and the per-tool description overrides.
`save_policy` and `load_policy` write and read it as YAML.
`load_active_policy` resolves a saved policy against the live strategy set, so a policy whose
strategy is no longer supported fails at load with a clear error.
The caller passes the policy file path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import strategy

if TYPE_CHECKING:
    from pathlib import Path


class RetrievalPolicy(BaseModel):
    """The instructions, strategy and tool descriptions a tuning loop tunes, as one frozen value.

    Attributes:
        instructions: The agent-under-test's system prompt.
        strategy: A retrieval-strategy name from the tuning-valid set (`strategy.EVAL_STRATEGY_NAMES`).
            `code-mode` is refused here, even though it stays a valid retrieval profile.
        tool_descriptions: Per-tool description overrides keyed by tool name. An empty mapping means
            the shipped tool descriptions apply verbatim.
    """

    model_config = ConfigDict(frozen=True)

    instructions: str
    strategy: str
    tool_descriptions: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("strategy")
    @classmethod
    def _strategy_must_be_eval_valid(cls, value: str) -> str:
        """Reject any strategy outside the tuning-valid set."""
        if value not in strategy.EVAL_STRATEGY_NAMES:
            msg = f"unknown eval retrieval strategy: {value!r} (expected one of {', '.join(strategy.EVAL_STRATEGY_NAMES)})"
            raise ValueError(msg)
        return value


def baseline_policy(instructions: str, *, strategy_name: str | None = None) -> RetrievalPolicy:
    """The shipped default policy a tuning loop starts from.

    Args:
        instructions: The consumer's default agent-under-test system instructions.
        strategy_name: The starting strategy name, or `None` for the default strategy (the full suite).

    Returns:
        The baseline `RetrievalPolicy`: the given instructions, the chosen strategy, and empty
        tool descriptions (so the shipped descriptions apply verbatim).
    """
    return RetrievalPolicy(instructions=instructions, strategy=strategy_name or strategy.DEFAULT_STRATEGY.value, tool_descriptions={})


def save_policy(policy: RetrievalPolicy, path: Path) -> None:
    """Write a `RetrievalPolicy` to `path` as YAML.

    `load_policy` reads back exactly what this writes.
    It creates `path`'s parent directory, so a caller may name a destination that does not exist yet.

    Args:
        policy: The policy to persist.
        path: The YAML destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def load_policy(path: Path) -> RetrievalPolicy:
    """Load a `RetrievalPolicy` from a YAML file (the inverse of `save_policy`).

    Args:
        path: The YAML file to load.

    Returns:
        The reconstructed policy, re-validated so a tampered file fails at the boundary.
    """
    return RetrievalPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class ActivePolicyResolution:
    """An active policy resolved against the live strategy set.

    Attributes:
        profile: The `RetrievalProfile` the policy's `strategy` resolves to, the tool subset and
            search leg a toolset builder uses.
        tool_descriptions: The policy's per-tool description overrides. Empty means the shipped
            descriptions apply verbatim.
        instructions: The policy's agent-under-test system prompt, used in place of the shipped
            default.
    """

    profile: strategy.RetrievalProfile
    tool_descriptions: Mapping[str, str]
    instructions: str


def _load_active_policy_file(policy_path: Path) -> RetrievalPolicy | None:
    """Load and validate the policy at `policy_path`, or return `None` when the file is absent."""
    if not policy_path.exists():
        return None
    policy = load_policy(policy_path)
    strategy.resolve_strategy(policy.strategy)
    return policy


def load_active_policy(policy_path: Path) -> ActivePolicyResolution | None:
    """Load the saved active policy and resolve its strategy against the live strategy set.

    It returns `None` when the file is absent. A policy whose strategy is no longer supported fails
    here with an error naming the supported strategies.

    Args:
        policy_path: The policy file to read.

    Returns:
        The typed `ActivePolicyResolution` when an active policy is present, else `None`.

    Raises:
        pydantic.ValidationError: When the file is not a valid `RetrievalPolicy` shape.
        ValueError: When the policy's `strategy` is not in the live strategy set.
    """
    policy = _load_active_policy_file(policy_path)
    if policy is None:
        return None
    profile = strategy.resolve_strategy(policy.strategy)
    return ActivePolicyResolution(profile=profile, tool_descriptions=policy.tool_descriptions, instructions=policy.instructions)


def effective_baseline_policy(policy_path: Path, *, default_instructions: str) -> RetrievalPolicy:
    """The policy a tuning loop starts from: the active one when present, else the shipped baseline.

    It returns the policy at `policy_path` when the file exists, else
    `baseline_policy(default_instructions)`. A malformed or stale file raises the same error as
    `load_active_policy` and never falls back.

    Args:
        policy_path: The policy file to read.
        default_instructions: The shipped default instructions used for the fallback baseline.

    Returns:
        The active `RetrievalPolicy` when present, else the shipped baseline.

    Raises:
        pydantic.ValidationError: When the file is not a valid `RetrievalPolicy` shape.
        ValueError: When the policy's `strategy` is not in the live strategy set.
    """
    policy = _load_active_policy_file(policy_path)
    if policy is None:
        return baseline_policy(default_instructions)
    return policy
