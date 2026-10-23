"""The shapes of the taxonomy and of what an inspection produces.

A `RootCause` is what can be wrong and where a fix for it can land. A `Symptom` is what can be seen going wrong in a
transcript and which root causes may be behind it. A `Taxonomy` is the whole set of both, and owns the checks no single row
can make. A `Finding` is one symptom seen in one transcript, with its evidence and its blames.

Every row is frozen and refuses unknown keys, so the taxonomy is a set of constants and a typo in a constructor is an error
at import time. The descriptions on the fields of `Finding`, `Blame` and `Surface` reach the JSON schema an inspector model
fills, and are written for that reader. `Span`, the pointer a finding uses, belongs to the transcript package.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.ai.transcripts.contracts import Span

ShowsIn = Literal["tool-call", "tool-return", "text", "thinking"]
CaughtBy = Literal["rule", "model", "rule_then_model"]
FoundBy = Literal["rule", "inspector"]
Horizon = Literal["moment", "run", "conversation"]

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", description="Identifier in snake case. Other rows refer to this row by it.")]


class SurfaceKind(StrEnum):
    """The kinds of thing a fix can edit. The user is not one of them, and neither is the world a tool runs against."""

    TOOL = "tool"
    SKILL = "skill"
    SYSTEM_PROMPT = "system_prompt"
    MODEL_TIER = "model_tier"


class RootCause(BaseModel):
    """One row of the root-cause list: what can be wrong, and the kinds of surface a fix for it can land on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    meaning: str = Field(min_length=1, description="What is wrong, in one sentence.")
    fix_lands_on: tuple[SurfaceKind, ...] = Field(min_length=1, description="The kinds of surface a fix for this can edit.")


class Symptom(BaseModel):
    """One row of the symptom list: what can be seen going wrong, and which root causes may be behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    shows_in: ShowsIn = Field(
        description="The pydantic-ai part kind a detector points at for it: the tool call the agent wrote, the tool's return "
        "(a retry that answers a call is that call's return), a text part, or a thinking part."
    )
    what_you_see: str = Field(min_length=1, description="The symptom as it appears in a transcript. The sentence an inspector matches on.")
    caught_by: CaughtBy = Field(
        description="Which detector catches it: a rule alone, a model alone, or a rule that flags and a model that decides."
    )
    horizon: Horizon = Field(
        description="How far the inspector looks first for a lead of this symptom: the flagged step, the whole run, or "
        "the conversation's earlier runs too."
    )
    possible_root_causes: tuple[RootCause, ...] = Field(
        min_length=1, description="The root causes that can be behind it. A blame on any other is refused."
    )

    @model_validator(mode="after")
    def root_causes_are_distinct(self) -> Self:
        """Refuse the same root cause listed twice."""
        names = [cause.name for cause in self.possible_root_causes]
        if len(names) != len(set(names)):
            msg = f"symptom {self.name!r} lists a root cause twice"
            raise ValueError(msg)
        return self


class Taxonomy(BaseModel):
    """Every root cause and every symptom, with the cross-checks between them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_causes: tuple[RootCause, ...] = Field(min_length=1)
    symptoms: tuple[Symptom, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def names_are_unique_and_every_reference_resolves(self) -> Self:
        """Refuse duplicate names and a symptom pointing at a root cause that is not in the list."""
        _refuse_duplicates("root cause", [cause.name for cause in self.root_causes])
        _refuse_duplicates("symptom", [symptom.name for symptom in self.symptoms])
        known = set(self.root_causes)
        for symptom in self.symptoms:
            for cause in symptom.possible_root_causes:
                if cause not in known:
                    msg = f"symptom {symptom.name!r} names root cause {cause.name!r}, which is not in the taxonomy"
                    raise ValueError(msg)
        return self

    def symptom(self, name: str) -> Symptom:
        """The symptom with this name. Raises `KeyError` when no symptom has it."""
        for symptom in self.symptoms:
            if symptom.name == name:
                return symptom
        msg = f"no symptom named {name!r}"
        raise KeyError(msg)

    def root_cause(self, name: str) -> RootCause:
        """The root cause with this name. Raises `KeyError` when no root cause has it."""
        for cause in self.root_causes:
            if cause.name == name:
                return cause
        msg = f"no root cause named {name!r}"
        raise KeyError(msg)

    def symptoms_shown_in(self, part: ShowsIn) -> tuple[Symptom, ...]:
        """The symptoms a detector reading one kind of part can be asked to look for."""
        return tuple(symptom for symptom in self.symptoms if symptom.shows_in == part)


def _refuse_duplicates(what: str, names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            msg = f"{what} {name!r} is defined twice"
            raise ValueError(msg)
        seen.add(name)


class Surface(BaseModel):
    """The thing a fix would edit: its kind and which one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SurfaceKind = Field(description="What kind of thing gets edited.")
    name: str = Field(
        default="",
        description="Which one: the tool's name, the skill's name, the tier's name. Empty when the kind has no instances to "
        "choose from, or when the tool does not exist yet.",
    )


class Blame(BaseModel):
    """One root cause behind a finding: what is wrong, where the fix lands, why we think so."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    what: RootCause = Field(description="What is wrong.")
    where: Surface = Field(description="The thing a fix would edit. Its kind must be one the root cause can be fixed on.")
    why: str = Field(min_length=1, description="One sentence pointing at the evidence in the transcript.")

    @model_validator(mode="after")
    def the_fix_lands_where_the_root_cause_allows(self) -> Self:
        """Refuse a pairing the root cause rules out, such as a tool description blamed on a skill."""
        if self.where.kind not in self.what.fix_lands_on:
            allowed = ", ".join(kind.value for kind in self.what.fix_lands_on)
            msg = f"{self.what.name} is never fixed on a {self.where.kind.value}: it lands on {allowed}"
            raise ValueError(msg)
        return self


class Finding(BaseModel):
    """One symptom seen in one transcript."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symptom: Symptom = Field(description="Which symptom.")
    evidence: tuple[Span, ...] = Field(min_length=1, description="Where in the transcript it shows. At least one pointer.")
    blames: tuple[Blame, ...] = Field(min_length=1, description="What is behind it, one entry per root cause. At least one.")
    found_by: FoundBy = Field(description="Which kind of detector found this instance: a rule, or the inspector.")
    explanation: str = Field(default="", description="What went wrong and what to fix, in plain sentences, when the detector wrote them.")

    @model_validator(mode="after")
    def every_blame_is_possible_for_the_symptom(self) -> Self:
        """Refuse a blame on a root cause the symptom does not list."""
        allowed = set(self.symptom.possible_root_causes)
        for blame in self.blames:
            if blame.what not in allowed:
                msg = f"{blame.what.name} cannot be behind {self.symptom.name}"
                raise ValueError(msg)
        return self
