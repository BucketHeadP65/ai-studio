"""A structural gate on the library's own text: nothing under `src/engram` names a source host.

engram is domain-general. A connector owns its host (its URL shapes, its key derivation, its
item-type vocabulary, its deep-link scheme) and lives in the `connectors` project, and a knowledge
base's own field names live in its template's schema. A host name in the indexer teaches the reader that engram
knows about one, which is exactly what every port here exists to prevent, and it is the surface
that drifts back first.

Every string in the module is checked, not only its prose: docstrings, comments, and string
constants alike, since a name emitted in a report or defaulted into a row reads to a person the
same way. Matched word-bounded, so an identifier that merely contains a host name (`arxiv_id`, a
real `sources_raw.meta` key and `api.papers` column, or the `arxiv_id_of` helper) reads as the code
reference it is. The one exempt module below is the recorded residue.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

from pathlib import Path

_LIBRARY_SRC = Path(__file__).resolve().parents[3] / "src" / "engram"

_SOURCE_HOST = re.compile(r"\b(?:zotero|youtube|arxiv)\b", re.IGNORECASE)
"""Host names the library must not carry: name the role (a connector, a video host, an identifier scheme)."""

_EXEMPT = {
    # The identifier helpers recognize two public identifier schemes by their own published URL
    # shapes, which no connector owns and which the source-queue classifier needs with none installed.
    _LIBRARY_SRC / "sources" / "domain" / "source_queue.py",
}


def _strings(path: Path) -> list[tuple[int, str]]:
    """Every string and comment in one module, as `(line number, text)` pairs.

    The strings are every `str` constant the module holds, which covers its docstrings (module,
    class, function and the attribute page_type this codebase documents constants with) and every
    literal a value or a message is built from.
    """
    text = path.read_text(encoding="utf-8")
    found = [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    found.extend(
        (token.start[0], token.string) for token in tokenize.generate_tokens(io.StringIO(text).readline) if token.type == tokenize.COMMENT
    )
    return found


def test_no_library_string_or_comment_names_a_source_host() -> None:
    offenders: list[str] = []
    for path in sorted(_LIBRARY_SRC.rglob("*.py")):
        if path in _EXEMPT:
            continue
        for lineno, text in _strings(path):
            offenders.extend(
                f"{path.relative_to(_LIBRARY_SRC)}:{lineno}: {line.strip()}" for line in text.splitlines() if _SOURCE_HOST.search(line)
            )
    assert not offenders, "the library names a source host:\n" + "\n".join(offenders)


def test_the_gate_reads_the_library_source_tree() -> None:
    """A wrong `_LIBRARY_SRC` would make the walk above pass over nothing at all."""
    assert (_LIBRARY_SRC / "indexing" / "service" / "index.py").is_file()
    assert len(list(_LIBRARY_SRC.rglob("*.py"))) > 100
