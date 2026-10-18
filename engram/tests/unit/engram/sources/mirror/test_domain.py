"""Unit tests for the mirror slice domain — only the bits that carry our own logic.

We test the enum's *serialized* string values (the on-disk YAML contract that
existing notes depend on), the deliberate `MirrorFile.converter` fallback default,
that `MirrorFile.provider` deliberately has none, and that the two value objects
are frozen. We do not re-test pydantic (field storage, declared defaults) — that
is third-party behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pydantic
import pytest

from engram.sources.contracts import MirrorFile, MirrorFrontmatter, MirrorStatus

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _frontmatter() -> MirrorFrontmatter:
    return MirrorFrontmatter(
        title="T",
        item_type="journalArticle",
        source_id="ABCD1234",
        provider="example-library",
        provider_key="ABCD1234",
        source_key="ABCD1234",
        source_version=7,
        source_link="example-library://items/ABCD1234",
        date_added=_EPOCH,
        date_modified=_EPOCH,
        pdf_path="/x.pdf",
        source_hash="deadbeef",
        converter="arxiv-html",
        synced=_EPOCH,
    )


def test_mirror_status_values_are_the_on_disk_strings() -> None:
    # These literals are written to frontmatter and parsed back; renaming them
    # would silently orphan every existing mirror note, so they are a contract.
    assert MirrorStatus.ACTIVE == "active"
    assert MirrorStatus.REMOVED == "removed"


def test_mirror_file_converter_defaults_to_unknown_for_legacy_notes() -> None:
    # A deliberate domain default, not a pydantic incidental: a note carrying no provenance label
    # must still load, reading back as "unknown".
    mirror_file = MirrorFile(
        source_key="ABCD1234", source_version=7, source_hash="deadbeef", provider="example-library", status=MirrorStatus.ACTIVE
    )

    assert mirror_file.converter == "unknown"


def test_mirror_frontmatter_is_frozen() -> None:
    # Documented value object: the immutable artifact is the DB sources_raw row,
    # but the model itself must reject in-place mutation so a value, once built,
    # is a stable record of what was written.
    frontmatter = _frontmatter()

    with pytest.raises(pydantic.ValidationError):
        frontmatter.title = "mutated"  # type: ignore[misc]


def test_mirror_file_is_frozen() -> None:
    mirror_file = MirrorFile(
        source_key="ABCD1234", source_version=7, source_hash="deadbeef", provider="example-library", status=MirrorStatus.ACTIVE
    )

    with pytest.raises(pydantic.ValidationError):
        mirror_file.source_version = 8  # type: ignore[misc]


def test_mirror_file_requires_a_provider() -> None:
    # A deliberate absence of a default: `provider` is what a sync run's orphan pass narrows its own
    # mirrors by, so a guessed name would archive another provider's mirrors.
    with pytest.raises(pydantic.ValidationError):
        MirrorFile.model_validate({"source_key": "ABCD1234", "source_version": 7, "source_hash": "deadbeef", "status": "active"})


def test_mirror_frontmatter_requires_a_provider() -> None:
    fields = {name: value for name, value in _frontmatter().model_dump().items() if name != "provider"}

    with pytest.raises(pydantic.ValidationError):
        MirrorFrontmatter.model_validate(fields)
