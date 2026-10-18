"""One pass of some rules over some transcripts, as one pydantic-evals run.

Each source is a case. The task opens the source into an `Inspected`, every rule appends its leads to it and returns
counts per symptom for the report, and the report carries those counts. A source the loader cannot open fails its case and
the others still run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_evals import Case, Dataset

from core.ai.inspection.detectors import Inspected, Rules
from core.ai.inspection.rules import RULES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic_evals.evaluators import Evaluator
    from pydantic_evals.reporting import EvaluationReport

    from core.ai.inspection.domain.facts import ToolFacts
    from core.ai.inspection.domain.leads import Lead
    from core.ai.transcripts.contracts import Transcript, TranscriptLoaders


@dataclass(frozen=True)
class RuleFailure:
    """One rule that raised while reading one transcript, and what it raised."""

    source: str
    rule: str
    message: str


@dataclass(frozen=True)
class RulesPassResult:
    """What one rules pass produced: every lead, every transcript it opened, every rule that raised, and the report."""

    leads: tuple[Lead, ...]
    inspected: Mapping[str, Inspected]
    rule_failures: tuple[RuleFailure, ...]
    report: EvaluationReport[str, Inspected, None]


class RulesPass:
    """Some rules, built once from the facts, ready to run over sources."""

    def __init__(  # noqa: PLR0913 (the pass's parts, each named where it is built)
        self,
        *,
        name: str,
        rules: Sequence[str],
        facts: ToolFacts,
        loaders: TranscriptLoaders,
        registry: Rules = RULES,
        opened: Mapping[str, Transcript] | None = None,
    ) -> None:
        """Resolve every rule name and build each rule from the facts.

        Args:
            name: The report's name.
            rules: The rule names, resolved in `registry` here, so an unknown name fails at construction.
            facts: What the rules need that a transcript does not carry.
            loaders: Opens a source into a transcript.
            registry: Where rule names resolve. The shipped rules by default.
            opened: Transcripts the caller already loaded, by source. A source listed here is not loaded again.

        Raises:
            KeyError: when a rule name is not in the registry.
        """
        self.name = name
        self.rules: tuple[Evaluator[str, Inspected, None], ...] = tuple(registry.select(rule)(facts) for rule in rules)
        self.loaders = loaders
        self.opened = dict(opened or {})

    async def run(self, sources: Sequence[str]) -> RulesPassResult:
        """Run every rule over every source once.

        Args:
            sources: The transcript sources, one case each.

        Returns:
            Every lead, every transcript opened, every rule that raised, and the report.
        """
        dataset = Dataset[str, Inspected, None](
            name=self.name, cases=[Case(name=source, inputs=source) for source in sources], evaluators=list(self.rules)
        )
        report = await dataset.evaluate(self._open, progress=False)
        inspected = {case.name: case.output for case in report.cases if case.output is not None}
        leads = tuple(lead for case in report.cases if case.output is not None for lead in case.output.leads)
        failures = tuple(
            RuleFailure(source=case.name, rule=failure.name, message=failure.error_message)
            for case in report.cases
            for failure in case.evaluator_failures
        )
        return RulesPassResult(leads=leads, inspected=inspected, rule_failures=failures, report=report)

    async def _open(self, source: str) -> Inspected:
        """Open `source` into a fresh `Inspected` for the rules to read and append leads to."""
        transcript = self.opened.get(source)
        return Inspected(transcript=transcript if transcript is not None else await self.loaders.load(source))
