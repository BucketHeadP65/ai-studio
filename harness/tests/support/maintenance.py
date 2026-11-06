"""The step maintenance steps a composition registers, by name, for a test that checks a registry holds them."""

from __future__ import annotations

MAINTENANCE_STEPS = (
    "list_consumers",
    "check_contracts",
    "run_step_cases",
    "prepare_plan",
    "land_changes",
    "report_changes",
    "select_disagreements",
    "repository_verdict",
    "hand_over_repository_changes",
)
"""The registered steps of step maintenance."""
