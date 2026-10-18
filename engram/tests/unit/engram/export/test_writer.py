r"""Unit tests for the filesystem bundle writer (`src/engram/export/infrastructure/writer.py`).

Two surfaces:

* :func:`render_concept` — the **pure** serialiser (frontmatter + body), asserted
  without touching disk, including the OKF v0.2 **conformance** round-trip: the text it
  emits parses back through :func:`core.markdown.split_frontmatter` to the same
  frontmatter, with a non-empty `type`.
* :class:`FilesystemBundleWriter` — the IO adapter, exercised against a real
  `tmp_path` (an adapter test legitimately writes files): it lays the bundle out as a
  directory tree (subdirs minted) and the root `index.md` carries `okf_version`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.markdown import split_frontmatter

from engram.export.contracts import OkfBundle, OkfConcept
from engram.export.infrastructure.writer import FilesystemBundleWriter, render_concept

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue


def _concept(path: str = "specimens/limpet.md", **front: JsonValue) -> OkfConcept:
    """An OKF concept with a sensible default frontmatter (type + the nested overlay keys)."""
    frontmatter: dict[str, JsonValue] = {
        "type": "specimen",
        "title": "Limpet",
        "description": "A grazing marine snail.",
        "tags": ["tidal", "survey"],
        "okf_x_relations": {"resembles": ["specimens/periwinkle"]},
        "okf_x_engram": {"id": "spc.limpet", "status": "draft", "collection": "fieldguide"},
        **front,
    }
    return OkfConcept(path=path, frontmatter=frontmatter, body="## Overview\n\nA snail.")


class TestRenderConcept:
    def test_frontmatter_round_trips_through_split_frontmatter(self):
        text = render_concept(_concept())

        data, body = split_frontmatter(text)
        assert data["type"] == "specimen"
        assert data["okf_x_relations"] == {"resembles": ["specimens/periwinkle"]}
        engram = data["okf_x_engram"]
        assert isinstance(engram, dict)
        assert engram["id"] == "spc.limpet"
        assert "## Overview" in body

    def test_emitted_type_is_non_empty(self):
        # The one OKF conformance rule the writer must never break.
        data, _ = split_frontmatter(render_concept(_concept()))
        assert isinstance(data["type"], str)
        assert data["type"]

    def test_blank_body_emits_frontmatter_only(self):
        text = render_concept(OkfConcept(path="x.md", frontmatter={"type": "t"}, body=""))

        data, body = split_frontmatter(text)
        assert data == {"type": "t"}
        assert body.strip() == ""


class TestFilesystemBundleWriter:
    def test_writes_concepts_and_index_as_a_directory_tree(self, tmp_path: Path):
        bundle = OkfBundle(
            concepts=(_concept(path="specimens/limpet.md"), _concept(path="records/coastal-survey.md", type="record")),
            index=OkfConcept(path="index.md", frontmatter={"okf_version": "0.2"}, body="# Bundle"),
        )

        FilesystemBundleWriter().write_bundle(bundle, str(tmp_path))

        assert (tmp_path / "specimens" / "limpet.md").is_file()
        assert (tmp_path / "records" / "coastal-survey.md").is_file()
        index_data, _ = split_frontmatter((tmp_path / "index.md").read_text(encoding="utf-8"))
        assert index_data["okf_version"] == "0.2"

    def test_written_concept_parses_back_to_its_type(self, tmp_path: Path):
        bundle = OkfBundle(
            concepts=(_concept(path="specimens/limpet.md"),),
            index=OkfConcept(path="index.md", frontmatter={"okf_version": "0.2"}, body="# Bundle"),
        )

        FilesystemBundleWriter().write_bundle(bundle, str(tmp_path))

        data, _ = split_frontmatter((tmp_path / "specimens" / "limpet.md").read_text(encoding="utf-8"))
        assert data["type"] == "specimen"
