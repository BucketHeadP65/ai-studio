"""`AuthoringTools.list_pages`: every page of one page_type by id, narrowed to one status or to the pages lacking one link when asked."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.testing.fakes.authoring import FakeAdminGateway
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from pathlib import Path


async def test_list_pages_lists_one_kind_by_id_with_title_and_status(tmp_path: Path) -> None:
    admin = FakeAdminGateway(
        rows=[
            {"page_id": "talk.a", "page_type": "talk", "title": "A", "status": "active"},
            {"page_id": "talk.b", "page_type": "talk", "title": "B", "status": "active"},
        ]
    )
    doors, _, _ = _doors(tmp_path, admin)

    rows = await doors.list_pages("research", "talk")

    assert rows == [
        {"page_id": "talk.a", "page_type": "talk", "title": "A", "status": "active"},
        {"page_id": "talk.b", "page_type": "talk", "title": "B", "status": "active"},
    ]
    sql, params = admin.calls[0]
    assert "p.page_type = :page_type" in sql
    assert "ORDER BY p.id" in sql
    assert "p.status = :status" not in sql, "no status asked, its clause is left out"
    assert ":lacking_link" not in sql, "no link asked, its clause is left out"
    assert params == {"collection": "research", "page_type": "talk", "limit": 500}


async def test_list_pages_narrows_to_one_status_and_caps_when_asked(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin)

    rows = await doors.list_pages("research", "flag", status="pending", limit=5)

    assert rows == []
    sql, params = admin.calls[0]
    assert "p.status = :status" in sql
    assert params == {"collection": "research", "page_type": "flag", "limit": 5, "status": "pending"}


async def test_list_pages_narrows_to_the_pages_lacking_an_outgoing_link_of_one_relation_when_asked(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin)

    await doors.list_pages("research", "paper", status="active", lacking_link="builds_on")

    sql, params = admin.calls[0]
    assert "NOT EXISTS" in sql
    assert "e.rel = :lacking_link" in sql
    assert "e.src_id = p.id" in sql, "only the page's own outgoing edges count"
    assert sql.index(":lacking_link") < sql.index("ORDER BY p.id")
    assert params == {"collection": "research", "page_type": "paper", "limit": 500, "status": "active", "lacking_link": "builds_on"}


async def test_list_pages_refuses_an_unknown_kind_naming_the_kinds(tmp_path: Path) -> None:
    admin = FakeAdminGateway(rows=[])
    doors, _, _ = _doors(tmp_path, admin)

    with pytest.raises(ModelRetry, match="no page_type 'nope' in knowledge base 'research', the page_types are"):
        await doors.list_pages("research", "nope")
    assert admin.calls == []
