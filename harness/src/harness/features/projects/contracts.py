"""What code outside the projects slice may use: the note overlay, the owner's preferences, the memory plugin and the skill catalog."""

from harness.features.projects.infrastructure.skill_files import BUILTIN_SKILL_DIRECTORY, catalog
from harness.features.projects.memory import memory_plugin_mounts, sanitize_segment
from harness.features.projects.note_overlay import NoteOverlay
from harness.features.projects.preferences import Preferences

__all__ = ["BUILTIN_SKILL_DIRECTORY", "NoteOverlay", "Preferences", "catalog", "memory_plugin_mounts", "sanitize_segment"]
