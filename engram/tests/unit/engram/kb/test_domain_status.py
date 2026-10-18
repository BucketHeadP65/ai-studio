"""Four-state disclosure lifecycle values are valid unit statuses."""

import pytest

from engram.kb.contracts import BaseFrontmatter
from engram.kb.domain.note import Status


@pytest.mark.parametrize("status", ["active", "superseded", "outdated", "pending"])
def test_disclosure_lifecycle_statuses_are_valid(status: Status) -> None:
    fm = BaseFrontmatter(id="mu.example", type="memory-unit", title="Example", status=status)
    assert fm.status == status
