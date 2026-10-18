"""A knowledge base's pending facet value proposals and their approval into its vocabulary.

A new facet value is proposed as a row in `_meta/vocabulary/pending.yaml`, never written straight into `_meta/vocabulary/facets.md`.
Approval merges it into `facets.md` through `engram.init.contracts.write_scaffold`.
Proposing, rejecting and listing touch only the pending file.
A proposal is keyed by `(facet, value)`, and every rewrite of the pending file is atomic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.cqrs.contracts import utcnow
from core.files import atomic_write

from engram.indexing.domain.vocab_proposal import VocabProposal
from engram.indexing.exceptions import ProposalUnknownError
from engram.init.contracts import write_scaffold

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

# The one vocabulary file a proposal can land in.
_FACETS_RELPATH = "_meta/vocabulary/facets.md"

# The frontmatter and H1 used only when the knowledge base has no `facets.md` yet.
_DEFAULT_FACETS_HEADER = (
    '---\nid: meta.vocabulary-facets\ntype: meta\ntitle: "Vocabulary: facet keys & controlled values"\n---\n\n'
    "# Vocabulary: facets"
)


def _facets_header(text: str) -> str:
    """The frontmatter, H1 and intro before the first `##` section, verbatim."""
    lines = text.splitlines()
    h2_idx = next((idx for idx, line in enumerate(lines) if line.startswith("## ")), len(lines))
    return "\n".join(lines[:h2_idx])


def _proposal_document(knowledge_base_root: Path, facet: str, value: str) -> str:
    """A `facets.md` holding only the proposed row, under the on-disk file's header when there is one."""
    existing_path = knowledge_base_root / _FACETS_RELPATH
    header = _facets_header(existing_path.read_text(encoding="utf-8")) if existing_path.is_file() else _DEFAULT_FACETS_HEADER
    cell = value.replace("|", "\\|")
    return f"{header}\n\n## `{facet}`: on all\n\n| Value | Definition |\n|---|---|\n| `{cell}` | proposed via vocab governance |\n"


class VocabGovernance:
    """Propose, list, approve and reject facet values over one knowledge base's `_meta/vocabulary/pending.yaml`.

    Approval also writes `_meta/vocabulary/facets.md`, where an existing entry always wins.
    Approving a value that is already registered leaves the vocabulary unchanged and still clears the pending row.
    """

    def __init__(self, knowledge_base_root: Path, *, clock: Callable[[], datetime] = utcnow) -> None:
        """Bind the proposals to one knowledge base.

        Args:
            knowledge_base_root: The knowledge base folder.
            clock: The clock that stamps a new proposal.
        """
        self._knowledge_base_root = knowledge_base_root
        self._pending_path = knowledge_base_root / "_meta" / "vocabulary" / "pending.yaml"
        self._clock = clock

    def pending(self) -> list[VocabProposal]:
        """Every pending proposal, oldest first.

        Returns:
            The proposals, empty before the first one.
        """
        if not self._pending_path.is_file():
            return []
        raw = yaml.safe_load(self._pending_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [VocabProposal.model_validate(row) for row in raw]

    def propose(self, *, facet: str, value: str, page_id: str) -> VocabProposal:
        """Record one `(facet, value)` proposal, or return the pending row for that pair unchanged.

        A pair already pending is not duplicated and keeps its first `page_id` and time.

        Args:
            facet: The facet key.
            value: The proposed value.
            page_id: The page that used the value.

        Returns:
            The new or existing proposal.
        """
        rows = self.pending()
        existing = next((row for row in rows if row.facet == facet and row.value == value), None)
        if existing is not None:
            return existing
        proposal = VocabProposal(facet=facet, value=value, page_id=page_id, proposed_at=self._clock())
        self._write([*rows, proposal])
        return proposal

    def reject(self, *, facet: str, value: str) -> None:
        """Remove the `(facet, value)` pending row. The vocabulary is never touched.

        Args:
            facet: The facet key.
            value: The proposed value.

        Raises:
            ProposalUnknownError: No pending row matches `(facet, value)`.
        """
        rows = self.pending()
        if not any(row.facet == facet and row.value == value for row in rows):
            raise ProposalUnknownError(facet, value)
        self._write([row for row in rows if not (row.facet == facet and row.value == value)])

    def approve(self, *, facet: str, value: str) -> None:
        """Merge `(facet, value)` into `_meta/vocabulary/facets.md`, then clear its pending row.

        Args:
            facet: The facet key.
            value: The proposed value.

        Raises:
            ProposalUnknownError: No pending row matches `(facet, value)`.
        """
        rows = self.pending()
        if not any(row.facet == facet and row.value == value for row in rows):
            raise ProposalUnknownError(facet, value)
        write_scaffold(self._knowledge_base_root, {_FACETS_RELPATH: _proposal_document(self._knowledge_base_root, facet, value)})
        self._write([row for row in rows if not (row.facet == facet and row.value == value)])

    def _write(self, rows: list[VocabProposal]) -> None:
        """Atomically rewrite the pending file with `rows`, creating its folder when needed."""
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump([row.model_dump(mode="json") for row in rows], sort_keys=False, allow_unicode=True)
        atomic_write(self._pending_path, payload)
