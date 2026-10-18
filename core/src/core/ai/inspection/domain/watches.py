"""A watch: the standing rule that says which runs get which rules, whether the inspector reads them, and when."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type When = Literal["run_end", "each_step", "sweep"]
type Reach = Literal["never", "when_a_rule_led", "every_run"]


class Watch(BaseModel):
    """Which runs, which rules, how far the inspector reads, when. Naming no agent and no session selects every run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    agent: str | None = Field(default=None, description="Only runs of the agent with this name.")
    session: str | None = Field(default=None, description="Only runs of this conversation.")
    rules: tuple[str, ...] = Field(default=(), description="The rules that read the run. May be empty when the inspector reads it.")
    inspector: Reach = Field(
        default="when_a_rule_led",
        description="How far the inspector reads: never, runs where a rule left a lead plus the configured clean-run sample, or every run whole.",
    )
    clean_run_sample_rate: float = Field(default=0.1, ge=0, le=1, description="Fraction of rule-free runs scanned under when_a_rule_led.")
    when: When = Field(default="run_end", description="On the run's end, on every step, or on the sweep timer.")
    sweep_hours: int | None = Field(default=None, ge=1, description="The window a sweep looks back over. Only with `when: sweep`.")
    reconcile_hours: int = Field(default=24, ge=1, description="How far the timer looks back for completed runs selected by a live watch.")

    @model_validator(mode="after")
    def a_watch_looks_at_something(self) -> Self:
        """Refuse a watch that names no rule unless the inspector reads every run.

        Returns:
            The watch.
        """
        if not self.rules and self.inspector != "every_run":
            msg = f"watch {self.name!r} names no rule, so only inspector: every_run would look at anything"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def a_sweep_has_a_window_and_nothing_else_does(self) -> Self:
        """Refuse a sweep without its window, and a window on a watch that does not sweep.

        Returns:
            The watch.
        """
        if (self.when == "sweep") != (self.sweep_hours is not None):
            msg = "sweep_hours is given exactly when `when` is sweep"
            raise ValueError(msg)
        return self

    def selects(self, *, agent_name: str | None, conversation_id: str | None) -> bool:
        """Whether a run with this agent and this conversation falls under the watch.

        Args:
            agent_name: The run's agent name.
            conversation_id: The run's conversation id.

        Returns:
            True when the watch selects the run.
        """
        if self.agent is not None and self.agent != agent_name:
            return False
        return self.session is None or self.session == conversation_id

    def samples(self, source: str) -> bool:
        """Whether `source` falls in this watch's clean-run sample.

        For one sample rate, the answer depends only on the watch name and the source.

        Args:
            source: The transcript source.

        Returns:
            True when the source is sampled.
        """
        digest = sha256(f"{self.name}\0{source}".encode()).digest()
        return int.from_bytes(digest, "big") < self.clean_run_sample_rate * (1 << 256)
