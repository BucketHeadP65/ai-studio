"""Which guidance the knowledge base speaks with, when two sources could answer.

The knowledge base has a routing doctrine its author wrote and nothing that a tuning run measured, so
today the deployment's distilled text is the whole of the guidance voice. That will not stay true:
the day somebody runs `studio evals apply-policy`, `_meta/active-policy.yaml` appears and should
displace a hand-distilled text immediately, without anyone remembering to come here and delete
this. The ordering is the behaviour, so the ordering is what these tests pin -- in both directions,
because a policy can be rolled back as easily as applied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from engram.evals.tuning.policy import baseline_policy

from harness.settings import KNOWLEDGE_BASE_GUIDANCE, knowledge_base_guidance

if TYPE_CHECKING:
    from pathlib import Path

APPLIED = "Prefer api.search with k <= 8; expand with api.edges only past two hops."


def _apply_policy(knowledge_base: Path, instructions: str) -> Path:
    """Write an active policy the way `studio evals apply-policy` does, and return its path."""
    policy = baseline_policy().model_copy(update={"instructions": instructions})
    path = knowledge_base / "_meta" / "active-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy.model_dump(mode="json")), encoding="utf-8")
    return path


def test_a_deployment_with_no_knowledge_base_still_speaks_its_own_doctrine() -> None:
    """A deployment restored from a database dump never clones the checkout, and it is still guided."""
    assert knowledge_base_guidance(None, KNOWLEDGE_BASE_GUIDANCE)() == KNOWLEDGE_BASE_GUIDANCE


def test_a_knowledge_base_with_no_applied_policy_falls_back_to_the_authored_doctrine(tmp_path: Path) -> None:
    """Today's real state: nothing has been tuned, and guidance a person wrote is still guidance."""
    assert knowledge_base_guidance(tmp_path, KNOWLEDGE_BASE_GUIDANCE)() == KNOWLEDGE_BASE_GUIDANCE


def test_an_applied_policy_supersedes_the_authored_doctrine(tmp_path: Path) -> None:
    """What a loop measured on this knowledge base beats what a person distilled by hand, the day it exists."""
    _apply_policy(tmp_path, APPLIED)
    assert knowledge_base_guidance(tmp_path, KNOWLEDGE_BASE_GUIDANCE)() == APPLIED


def test_the_two_sources_are_resolved_per_request_not_at_composition(tmp_path: Path) -> None:
    """Both directions: a policy applied mid-process takes over, and one rolled back hands the floor back."""
    speak = knowledge_base_guidance(tmp_path, KNOWLEDGE_BASE_GUIDANCE)
    assert speak() == KNOWLEDGE_BASE_GUIDANCE

    policy = _apply_policy(tmp_path, APPLIED)
    assert speak() == APPLIED

    policy.unlink()
    assert speak() == KNOWLEDGE_BASE_GUIDANCE, "a rolled-back policy steps the knowledge base guided, not silent"


def test_the_authored_guidance_speaks_the_read_tools_language() -> None:
    """The agent holds `search` and `fetch`, so guidance naming SQL surfaces would teach tools it lacks.

    The negative half is the one worth pinning: the old routing doctrine was distilled for the SQL
    plugin (`api.search`, `api.claims`), and any of that vocabulary surviving here would send the
    agent reaching for statements it can no longer run.
    """
    assert "api." not in KNOWLEDGE_BASE_GUIDANCE
    assert "SQL" not in KNOWLEDGE_BASE_GUIDANCE


def test_the_standing_rules_survive_the_distillation() -> None:
    """A compression that dropped these would have kept the words and lost the discipline."""
    assert "saturation" in KNOWLEDGE_BASE_GUIDANCE
    assert "summaries" in KNOWLEDGE_BASE_GUIDANCE, "read a hit's summaries before its body"
    assert "a suggestion" in KNOWLEDGE_BASE_GUIDANCE, "budgets are doctrine, not a limit"
