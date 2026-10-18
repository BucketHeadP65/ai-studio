"""The file adapter that writes an index run's validation report into a knowledge base's `_meta/dashboard.md`."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from core.files import atomic_write

if TYPE_CHECKING:
    from pathlib import Path

    from engram.indexing.contracts import ValidationIssue, ValidationReport


class FileValidationReportSink:
    """Writes the index validation report into the validation block of the knowledge base's `_meta/dashboard.md`.

    It touches only the block between the validation markers, adds the block once at the end of the page when it is absent, and
    leaves every other section untouched. Each write is atomic, so a reader never sees a half-written page.
    """

    def __init__(self, knowledge_base_path: Path) -> None:
        """Point the sink at one knowledge base.

        Args:
            knowledge_base_path: The knowledge base's root folder.
        """
        self._dashboard = knowledge_base_path / "_meta" / "dashboard.md"

    async def write(self, report: ValidationReport) -> None:
        """Replace the validation block with the rendered report, or append the block when the page has none.

        Args:
            report: The validation report of one index run.
        """
        existing = self._dashboard.read_text(encoding="utf-8") if self._dashboard.exists() else ""
        atomic_write(self._dashboard, _splice_validation_block(existing, _render_validation_block(report)))


# The markers around the validation block. Everything between them is rewritten on each index, everything outside is kept byte for byte.
_VALIDATION_BEGIN = "<!-- INDEX-VALIDATION:BEGIN (managed by `studio index` — do not edit by hand) -->"
_VALIDATION_END = "<!-- INDEX-VALIDATION:END -->"

# Any validation block, whatever label its BEGIN marker carries.
_VALIDATION_REGION = re.compile(r"<!-- INDEX-VALIDATION:BEGIN[^\n]*-->\n.*?<!-- INDEX-VALIDATION:END -->", re.DOTALL)

# How many issues to list under each severity before summarising the rest.
_MAX_LISTED = 20


def _splice_validation_block(existing: str, block: str) -> str:
    """Return `existing` with its first validation block replaced by `block` and any later ones dropped, or with `block` appended."""
    region = f"{_VALIDATION_BEGIN}\n{block}\n{_VALIDATION_END}"
    matches = list(_VALIDATION_REGION.finditer(existing))
    if not matches:
        lead = "\n\n" if existing and not existing.endswith("\n\n") else ""
        return f"{existing.rstrip(chr(10))}{lead}{region}\n" if existing else f"{region}\n"
    first = matches[0]
    tail = existing[first.end() :]
    for later in reversed(matches[1:]):
        start, end = later.start() - first.end(), later.end() - first.end()
        tail = tail[:start] + tail[end:]
    tail = re.sub(r"\n{3,}", "\n\n", tail)
    if not tail.strip():
        tail = "\n"
    return existing[: first.start()] + region + tail


def _render_validation_block(report: ValidationReport) -> str:
    """Render a validation report as the markdown body of the validation block."""
    lines = ["## Indexer validation", "", f"- **Errors:** {report.n_errors}", f"- **Warnings:** {report.n_warnings}"]
    if report.ingest_queue:
        lines.append(f"- **Ingest queue ({len(report.ingest_queue)}):** {', '.join(report.ingest_queue)}")
    lines.extend(_render_issue_list("Errors", report.errors))
    lines.extend(_render_issue_list("Warnings", report.warnings))
    return "\n".join(lines)


def _render_issue_list(title: str, issues: tuple[ValidationIssue, ...]) -> list[str]:
    """Render one severity's issues as a titled bullet list, capped, with an overflow line."""
    if not issues:
        return []
    out = ["", f"### {title}"]
    out.extend(f"- `{issue.page_id}`{_anchor(issue)} — {issue.rule}: {issue.message}" for issue in issues[:_MAX_LISTED])
    if len(issues) > _MAX_LISTED:
        out.append(f"- …and {len(issues) - _MAX_LISTED} more")
    return out


def _anchor(issue: ValidationIssue) -> str:
    """The `^cN` anchor suffix of a claim-level issue, or empty for a page-level one."""
    return f" {issue.anchor}" if issue.anchor else ""
