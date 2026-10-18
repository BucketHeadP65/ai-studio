"""What code outside the authoring slice may use: the draft, the agent build and the plugins the composition mounts."""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

from harness.features.authoring.domain import Draft
from harness.features.authoring.internal_contracts import LaunchWorkflow

if TYPE_CHECKING:
    from harness.features.authoring.agent import CHAT_BUDGET, build_agent
    from harness.features.authoring.digest_now import digest_now_tool
    from harness.features.authoring.process import WorkflowProcess
    from harness.features.authoring.skill_hint import skill_hint_capability
    from harness.features.authoring.workers import run_external_agent_step

__all__ = ["CHAT_BUDGET", "Draft", "LaunchWorkflow", "WorkflowProcess", "build_agent", "digest_now_tool", "run_external_agent_step", "skill_hint_capability"]

_LAZY_IMPORTS = {
    "CHAT_BUDGET": "harness.features.authoring.agent",
    "WorkflowProcess": "harness.features.authoring.process",
    "build_agent": "harness.features.authoring.agent",
    "digest_now_tool": "harness.features.authoring.digest_now",
    "run_external_agent_step": "harness.features.authoring.workers",
    "skill_hint_capability": "harness.features.authoring.skill_hint",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
