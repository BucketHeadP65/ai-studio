"""`migrate_map_turns._raw_page`: a page removed between the listing and the read is skipped, not fatal.

`scripts/` sits outside the package proper (see the script's own module docstring), reached here
the same way `pytest`'s own `pythonpath` reaches it -- a plain module import, no package install.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from scripts.migrate_map_turns import RowFetcher, _migrate_one, _raw_page, _scan_for_leftovers

if TYPE_CHECKING:
    from collections.abc import Mapping

_A_ROW: dict[str, object] = {
    "id": "wf.still-here",
    "type": "workflow",
    "title": "Still Here",
    "summary": "",
    "status": "draft",
    "applicability": "",
    "frontmatter": {},
}
"""A page with no `spec` in its long tail -- enough to prove `_migrate_one` reaches its own
report line for a page that was actually read, distinct from one that never was."""


@dataclass
class FakeRowFetcher:
    """A hand-written stand-in for `RowFetcher`, scripted per workflow id.

    An id absent from `rows` answers no rows at all -- the same as a page removed from the store
    between the listing that named it and this read.
    """

    rows: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    async def fetch(self, _query: str, /, *args: object) -> list[dict[str, object]]:
        workflow_id = str(args[0]) if args else ""
        return list(self.rows.get(workflow_id, []))


async def test_raw_page_answers_none_for_a_page_removed_since_it_was_listed() -> None:
    pool: RowFetcher = FakeRowFetcher()

    assert await _raw_page(pool, "wf.gone") is None


async def test_raw_page_still_answers_the_row_for_a_page_that_is_there() -> None:
    pool: RowFetcher = FakeRowFetcher(rows={"wf.still-here": [_A_ROW]})

    raw = await _raw_page(pool, "wf.still-here")

    assert raw is not None
    page, long_tail = raw
    assert page["id"] == "wf.still-here"
    assert long_tail == {}


async def test_a_vanished_page_is_skipped_and_the_report_names_it_and_the_run_continues() -> None:
    """A page gone since the listing used to raise a bare `IndexError` and kill the whole migration.

    The fix reports the id and moves on -- proved here by driving `_migrate_one` the same way
    `main`'s own loop drives it, one id after another.
    """
    pool: RowFetcher = FakeRowFetcher(rows={"wf.still-here": [_A_ROW]})

    async def unreached_ingest(payload: Mapping[str, Any], *, collection: str) -> None:
        """Never called: both scripted ids here return before reaching `_reingest`."""
        del payload, collection
        msg = "ingest should not be called for a page that was never actually read"
        raise AssertionError(msg)

    reports = [await _migrate_one(pool, unreached_ingest, workflow_id) for workflow_id in ("wf.gone", "wf.still-here")]

    assert reports[0] == "wf.gone: vanished since it was listed, skipped"
    assert reports[1] == "wf.still-here: no spec on this page, left alone"


async def test_scan_for_leftovers_skips_a_vanished_page_rather_than_raising() -> None:
    pool: RowFetcher = FakeRowFetcher(rows={"wf.still-here": [_A_ROW]})

    leftovers = await _scan_for_leftovers(pool, ["wf.gone", "wf.still-here"])

    assert leftovers == []
