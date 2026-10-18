"""The public surface of the skills engram ships."""

from pathlib import Path

SKILLS = Path(__file__).parent
"""The directory holding one folder per shipped skill, each with its SKILL.md."""

__all__ = ["SKILLS"]
