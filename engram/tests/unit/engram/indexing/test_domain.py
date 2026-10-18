"""Unit tests for `MirrorDoc.from_text` (`index/domain.py`): deriving a mirror's identity key.

`MirrorDoc` is index's own read of a `Sources/*.md` mirror, straight off the raw parsed
frontmatter mapping (never through `engram.sources.domain.mirror`'s pydantic models). The one field
name it reads is `source_key`, the name the mirror store writes.
"""

from __future__ import annotations

from engram.indexing.contracts import MirrorDoc
from engram.indexing.domain.models import loadable_mirrors


def test_from_text_reads_the_source_key_field() -> None:
    text = "---\nsource_key: ABCD1234\ntitle: T\n---\nbody\n"

    mirror = MirrorDoc.from_text("Sources/Some Title.md", text, "research")

    assert mirror.source_key == "ABCD1234"


def test_from_text_reads_no_other_field_name_as_the_key() -> None:
    # A mirror carrying only some other knowledge base's or provider's key field has no `source_key`, so it
    # falls through to the filename stem rather than picking that field up.
    text = "---\nzotero_key: LEGACY01\ntitle: T\n---\nbody\n"

    mirror = MirrorDoc.from_text("Sources/Legacy Paper.md", text, "research")

    assert mirror.source_key == "Legacy Paper"


def test_from_text_falls_back_to_the_filename_stem_when_the_key_is_absent() -> None:
    text = "---\ntitle: T\n---\nbody\n"

    mirror = MirrorDoc.from_text("Sources/No Key At All.md", text, "research")

    assert mirror.source_key == "No Key At All"


def test_from_text_falls_back_to_the_filename_stem_when_the_key_is_blank() -> None:
    text = "---\nsource_key: ''\ntitle: T\n---\nbody\n"

    mirror = MirrorDoc.from_text("Sources/Blank Key.md", text, "research")

    assert mirror.source_key == "Blank Key"


def test_from_text_keeps_the_frontmatter_mapping_and_splits_the_body() -> None:
    text = "---\nsource_key: ABCD1234\ntitle: T\n---\n# Heading\n\nbody text\n"

    mirror = MirrorDoc.from_text("Sources/Some Title.md", text, "research")

    assert mirror.frontmatter == {"source_key": "ABCD1234", "title": "T"}
    assert mirror.body == "# Heading\n\nbody text\n"
    assert mirror.text == text
    assert mirror.collection == "research"
    assert mirror.path == "Sources/Some Title.md"


def test_provider_is_the_name_the_mirror_declares() -> None:
    text = "---\nsource_key: ABCD1234\nprovider: example-library\n---\nbody\n"

    assert MirrorDoc.from_text("Sources/A.md", text, "research").provider == "example-library"


def test_provider_is_empty_when_the_mirror_declares_none() -> None:
    text = "---\nsource_key: ABCD1234\n---\nbody\n"

    assert MirrorDoc.from_text("Sources/A.md", text, "research").provider == ""


def test_provider_is_empty_when_the_declared_name_is_blank() -> None:
    text = "---\nsource_key: ABCD1234\nprovider: '   '\n---\nbody\n"

    assert MirrorDoc.from_text("Sources/A.md", text, "research").provider == ""


def test_loadable_mirrors_keeps_only_the_ones_declaring_a_provider() -> None:
    keyed = MirrorDoc.from_text("Sources/keyed.md", "---\nsource_key: A\nprovider: example-library\n---\nbody\n", "research")
    bare = MirrorDoc.from_text("Sources/bare.md", "---\nsource_key: B\n---\nbody\n", "research")

    assert loadable_mirrors((keyed, bare)) == (keyed,)


def test_loadable_mirrors_keeps_the_order_it_was_given() -> None:
    first = MirrorDoc.from_text("Sources/one.md", "---\nsource_key: A\nprovider: p\n---\nbody\n", "research")
    second = MirrorDoc.from_text("Sources/two.md", "---\nsource_key: B\nprovider: p\n---\nbody\n", "research")

    assert loadable_mirrors((second, first)) == (second, first)
