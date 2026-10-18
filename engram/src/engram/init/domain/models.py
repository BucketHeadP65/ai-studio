"""Domain models for the init feature, what a scaffold write run produced."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InitReport(BaseModel):
    """Summary of what a `write_scaffold` run did.

    Attributes:
        written: knowledge-base-relative paths newly created on disk.
        skipped: knowledge-base-relative paths that existed and were not force-overwritten.
        merged: knowledge-base-relative registry paths where rows were union-merged (existing definitions preserved).
        forced: knowledge-base-relative paths overwritten because `force=True` was set.
    """

    model_config = ConfigDict(frozen=True)

    written: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    merged: tuple[str, ...] = ()
    forced: tuple[str, ...] = ()

    def summary(self) -> str:
        """Return a one-paragraph CLI-friendly summary of what was done.

        Returns:
            Multi-line string listing counts per action category.
        """
        lines: list[str] = []
        if self.written:
            lines.append(f"  created  : {len(self.written)} file(s)")
        if self.merged:
            lines.append(f"  merged   : {len(self.merged)} registry file(s) (human edits preserved)")
        if self.forced:
            lines.append(f"  replaced : {len(self.forced)} file(s) (--force)")
        if self.skipped:
            lines.append(f"  skipped  : {len(self.skipped)} file(s) (already exist, use --force to overwrite)")
        if not lines:
            lines.append("  nothing to do: all files already up to date")
        return "\n".join(lines)
