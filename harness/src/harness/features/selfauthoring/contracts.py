"""What code outside the self-authoring slice may use: the agent's creation tools, the custom capabilities, the selection and the CLI.

Everything but `SelectionError` and `CUSTOM_ITEM_DIRECTORIES` loads on first use, so `import harness.cli` loads none of the custom
items, and `harness.session` can import this module while the modules behind it reach `harness.session`.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

from harness.features.selfauthoring.domain import CUSTOM_ITEM_DIRECTORIES
from harness.features.selfauthoring.exceptions import SelectionError

if TYPE_CHECKING:
    from harness.features.selfauthoring.api_models import Selection, TierBody
    from harness.features.selfauthoring.infrastructure.activation_log import activations_under
    from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
    from harness.features.selfauthoring.infrastructure.promotion import promoted_capabilities_digest, promoted_capability_types
    from harness.features.selfauthoring.routes_cli import fail, harness_app
    from harness.features.selfauthoring.service import select_profile, selected_profile_name, selected_profile_skills
    from harness.features.selfauthoring.skills_plugin import SkillsPlugin, known_skill_names
    from harness.features.selfauthoring.tools import (
        AGENT_GUIDANCE,
        PENDING_GUIDANCE,
        STEP_GUIDANCE,
        AgentCreation,
        PendingCapabilityCreation,
        ProfileCreation,
        ProfileVoice,
        StepCreation,
    )

__all__ = [
    "AGENT_GUIDANCE",
    "CUSTOM_ITEM_DIRECTORIES",
    "PENDING_GUIDANCE",
    "STEP_GUIDANCE",
    "AgentCreation",
    "FileCustomCapabilities",
    "PendingCapabilityCreation",
    "ProfileCreation",
    "ProfileVoice",
    "Selection",
    "SelectionError",
    "SkillsPlugin",
    "StepCreation",
    "TierBody",
    "activations_under",
    "fail",
    "harness_app",
    "known_skill_names",
    "promoted_capabilities_digest",
    "promoted_capability_types",
    "select_profile",
    "selected_profile_name",
    "selected_profile_skills",
]

_LAZY_IMPORTS = {
    "AGENT_GUIDANCE": "harness.features.selfauthoring.tools",
    "AgentCreation": "harness.features.selfauthoring.tools",
    "FileCustomCapabilities": "harness.features.selfauthoring.infrastructure.capabilities",
    "PENDING_GUIDANCE": "harness.features.selfauthoring.tools",
    "PendingCapabilityCreation": "harness.features.selfauthoring.tools",
    "ProfileCreation": "harness.features.selfauthoring.tools",
    "ProfileVoice": "harness.features.selfauthoring.tools",
    "STEP_GUIDANCE": "harness.features.selfauthoring.tools",
    "Selection": "harness.features.selfauthoring.api_models",
    "SkillsPlugin": "harness.features.selfauthoring.skills_plugin",
    "StepCreation": "harness.features.selfauthoring.tools",
    "TierBody": "harness.features.selfauthoring.api_models",
    "activations_under": "harness.features.selfauthoring.infrastructure.activation_log",
    "fail": "harness.features.selfauthoring.routes_cli",
    "harness_app": "harness.features.selfauthoring.routes_cli",
    "known_skill_names": "harness.features.selfauthoring.skills_plugin",
    "promoted_capabilities_digest": "harness.features.selfauthoring.infrastructure.promotion",
    "promoted_capability_types": "harness.features.selfauthoring.infrastructure.promotion",
    "select_profile": "harness.features.selfauthoring.service",
    "selected_profile_name": "harness.features.selfauthoring.service",
    "selected_profile_skills": "harness.features.selfauthoring.service",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
