"""The names the step maintenance workflow and its watches agree on."""

from __future__ import annotations

MAINTENANCE_WORKFLOW = "step-maintenance"
"""The name of the workflow that checks a new reusable step version against each consumer."""

PLAN_GATE = "judge_plan"
"""The id of the step maintenance workflow's judge gate over its plan. A run that spends its last attempt lands nothing."""

CHANGED_STEPS = "changed_step_ids"
"""The step maintenance workflow's parameter: the changed step versions' ids, comma-separated."""
