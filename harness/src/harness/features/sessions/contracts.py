"""What code outside the sessions slice may use: the sessions port, the projection, the registry of projects, project configuration and friction flags."""

from harness.features.sessions import project_registry
from harness.features.sessions.api_models import SessionSummary, UsageTotals
from harness.features.sessions.flags import FrictionFlagging, friction_lines
from harness.features.sessions.importing import import_file_store
from harness.features.sessions.internal_contracts import Sessions
from harness.features.sessions.project_config import CompactionConfig, ProjectConfig, SetupConfig, project_config
from harness.features.sessions.service import SessionProjection, project_id_of, sweep_projects, title_for

__all__ = [
    "CompactionConfig",
    "FrictionFlagging",
    "ProjectConfig",
    "SessionProjection",
    "SessionSummary",
    "Sessions",
    "SetupConfig",
    "UsageTotals",
    "friction_lines",
    "import_file_store",
    "project_config",
    "project_id_of",
    "project_registry",
    "sweep_projects",
    "title_for",
]
