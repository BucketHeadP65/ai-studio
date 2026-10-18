"""Rewrite the evidence file's approval records into the new word.

The evidence file stores one compact JSON record per line. A record of kind `"blessing"` becomes `"kind": "approval"`
with every other key unchanged, so a workflow's trust level reads what it read before. Run it with
`uv run python -m harness.migrations.rewrite_trust_words`, or let `harness serve` run it at boot. A second run finds
nothing and says so.
"""

from __future__ import annotations

import asyncio
import json

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness.migrations.report import MigrationReport
from harness.settings import StateSettings

if TYPE_CHECKING:
    from pathlib import Path

_OLD_KIND = "blessing"
_NEW_KIND = "approval"


@dataclass
class TrustWordsReport(MigrationReport):
    """What one run of the trust words rewrite did."""

    approvals: int = 0
    """How many evidence records were rewritten to the new kind."""

    @property
    def touched(self) -> bool:
        """Whether anything at all was rewritten."""
        return bool(self.approvals)

    def lines(self) -> list[str]:
        """One line saying what was rewritten, or that there was nothing to do.

        Returns:
            The lines to print, in order.
        """
        if not self.touched:
            return ["nothing to migrate: every evidence record already reads the new words"]
        return [f"rewrote {self.approvals} approval record(s) in the evidence file"]


def _rewrite(path: Path, report: TrustWordsReport) -> None:
    """Rewrite `path`'s records of kind `"blessing"` in place, compact like the log writes them, copying every other line through."""
    if not path.exists():
        return
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(record, dict) and record.get("kind") == _OLD_KIND:
            out.append(json.dumps({**record, "kind": _NEW_KIND}, separators=(",", ":"), ensure_ascii=False))
            report.approvals += 1
            continue
        out.append(line)
    if report.approvals:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")


async def migrate(state: StateSettings) -> TrustWordsReport:
    """Rewrite the evidence file under `state` into the new trust words.

    Args:
        state: The state settings that name the evidence file.

    Returns:
        What the run rewrote.
    """
    report = TrustWordsReport()
    _rewrite(state.evidence_log, report)
    return report


def main() -> None:
    """Run the rewrite over the default state home and print what it did."""
    for line in asyncio.run(migrate(StateSettings())).lines():
        print(line)  # noqa: T201 (a command line tool prints its report)


if __name__ == "__main__":
    main()
