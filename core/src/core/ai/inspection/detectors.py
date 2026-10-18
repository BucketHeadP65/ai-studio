"""What every rule shares: the inspected transcript it reads and writes leads into, and the registry it is built from.

A rule is a pydantic-evals evaluator over one case whose input is a transcript source and whose output is an `Inspected`.
The output is shared by every rule of the case, so a rule appends its leads to it and returns counts per symptom for the
report. Rules are built from the tool facts at inspection time, so a rule that needs a fact takes it as a field.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from core.ai.inspection.domain.facts import ToolFacts
from core.ai.inspection.domain.leads import Lead
from core.ai.registry import Registry
from core.ai.transcripts.contracts import Transcript

if TYPE_CHECKING:
    from core.ai.inspection.domain.taxonomy import Symptom
    from core.ai.transcripts.contracts import Span


@dataclass
class Inspected:
    """One transcript under inspection and the leads the rules have seen in it so far."""

    transcript: Transcript
    leads: list[Lead] = field(default_factory=list)


type InspectionContext = EvaluatorContext[str, Inspected, None]
"""What a rule receives: `ctx.inputs` is the source, `ctx.output` the inspected transcript."""

type RuleFactory = Callable[[ToolFacts], Evaluator[str, Inspected, None]]
"""Builds one rule from the facts. A rule that needs no fact ignores them."""


class Rules(Registry[str, RuleFactory]):
    """The rules by name."""


def record_lead(ctx: InspectionContext, symptom: Symptom, spans: Sequence[Span], *, found_by: str) -> None:
    """Record that `symptom` shows at `spans` in the transcript under inspection.

    Args:
        ctx: The rule's context, whose output receives the lead.
        symptom: The symptom seen.
        spans: Where in the transcript it shows.
        found_by: The name of the rule that saw it.
    """
    ctx.output.leads.append(Lead(source=ctx.output.transcript.source, symptom=symptom.name, spans=tuple(spans), found_by=found_by))
