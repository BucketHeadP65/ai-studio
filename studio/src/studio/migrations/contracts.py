"""What code outside the migrations may use: the migrations `studio serve-http` runs at boot."""

from studio.migrations import wave3, wave4

BOOT_MIGRATIONS = (("wave3", wave3.needs_migration, wave3.migrate), ("wave4", wave4.needs_migration, wave4.migrate))
"""Each migration boot runs, in order, as its log label, the check that says it has work, and the run that does it."""

__all__ = ["BOOT_MIGRATIONS"]
