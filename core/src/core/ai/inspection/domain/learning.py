"""The kernel's learning records, specialized on the inspection's task execution."""

from __future__ import annotations

from core.ai.evaluation.contracts import LearningAttempt as _LearningAttempt
from core.ai.evaluation.contracts import LearningRecord as _LearningRecord
from core.ai.inspection.domain.evaluation import TaskExecution

LearningAttempt = _LearningAttempt[TaskExecution]
LearningRecord = _LearningRecord[TaskExecution]
