"""`_splice_validation_block`: the dashboard's validation region is replaced in place, never accumulated."""

from __future__ import annotations

from studio.knowledge_base.indexing.infrastructure.validation_report_sink import (
    _VALIDATION_BEGIN,
    _VALIDATION_END,
    _splice_validation_block,
)

_OTHER_MANAGER_BEGIN = "<!-- INDEX-VALIDATION:BEGIN (managed by something else — do not edit by hand) -->"


def _region(body: str, begin: str = _VALIDATION_BEGIN) -> str:
    return f"{begin}\n{body}\n{_VALIDATION_END}"


def test_a_page_without_a_block_gets_one_appended() -> None:
    assert _splice_validation_block("# Health\n\nprose\n", "new") == f"# Health\n\nprose\n\n{_region('new')}\n"
    assert _splice_validation_block("", "new") == f"{_region('new')}\n"


def test_the_block_is_replaced_in_place_on_the_next_index() -> None:
    existing = f"# Health\n\n{_region('old')}\n\ntrailing prose\n"

    assert _splice_validation_block(existing, "new") == f"# Health\n\n{_region('new')}\n\ntrailing prose\n"


def test_a_block_from_an_older_manager_is_replaced_not_joined() -> None:
    """A page carrying a block another manager wrote: it is the same region, so it is taken over, not kept beside ours."""
    existing = f"# Health\n\nprose\n\n{_region('old', _OTHER_MANAGER_BEGIN)}\n"

    out = _splice_validation_block(existing, "new")

    assert out == f"# Health\n\nprose\n\n{_region('new')}\n"
    assert out.count("INDEX-VALIDATION:BEGIN") == 1


def test_accumulated_blocks_collapse_to_one() -> None:
    existing = f"prose\n\n{_region('old', _OTHER_MANAGER_BEGIN)}\n\n{_region('old2')}\n\n{_region('old3')}\n"

    out = _splice_validation_block(existing, "new")

    assert out == f"prose\n\n{_region('new')}\n"
