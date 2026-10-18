"""Idempotent filesystem writer: materialise scaffold output into a knowledge base directory.

`write_scaffold` creates, skips, merges, or force-overwrites each file according
to whether it already exists and whether it is a vocabulary file (where a
union-merge preserves human edits) or a prose/template file (where the default
is skip-existing to avoid clobbering curated content).
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from core.files import atomic_write, guarded_child

from engram.init.domain.models import InitReport
from engram.kb.contracts import parse_vocabulary

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from engram.kb.contracts import RelationSpec, VocabularyIndex

# The three vocabulary file names that receive union-merge treatment.
_REGISTRY_FILES = frozenset({"_meta/vocabulary/entity-types.md", "_meta/vocabulary/relation-types.md", "_meta/vocabulary/facets.md"})
# A facet section's H2 heading, which names its facet in backticks right after the `## `.
_FACET_HEADING = re.compile(r"^## \s*`(?P<name>[^`]+)`")
# A backtick-wrapped value, which marks a table row as a value row when it sits in the row's first cell.
_BACKTICK_VALUE = re.compile(r"`[^`]+`")
# The facet whose values are the claim kinds.
_CLAIM_KIND_FACET = "kind"


def write_scaffold(knowledge_base_root: Path, files: dict[str, str], *, force: bool = False) -> InitReport:
    """Write scaffold files into `knowledge_base_root` idempotently.

    A missing file is written. An existing vocabulary file is union-merged, so existing definitions
    win and new rows are appended. Any other existing file is overwritten when `force` is set and
    skipped otherwise. Every write is atomic, so a crash never leaves a truncated file. A `relpath`
    that would write outside `knowledge_base_root` is refused.

    Args:
        knowledge_base_root: Absolute path to the knowledge base root directory.
        files: Mapping of knowledge-base-relative POSIX paths to file text, as returned
            by `scaffold()`.
        force: If True, overwrite existing non-vocabulary files.  Vocabulary files
            always use union-merge regardless of this flag.

    Returns:
        An `InitReport` summarising what was created, skipped, merged, or forced.

    Raises:
        ValueError: A `relpath` is absolute or contains a `..` component that would write
            outside `knowledge_base_root`.
    """
    written: list[str] = []
    skipped: list[str] = []
    merged: list[str] = []
    forced: list[str] = []

    for relpath, text in files.items():
        target = guarded_child(knowledge_base_root, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            atomic_write(target, text)
            written.append(relpath)
        elif relpath in _REGISTRY_FILES:
            merged_text = _merge_vocabulary_file(relpath, target.read_text(encoding="utf-8"), text)
            atomic_write(target, merged_text)
            merged.append(relpath)
        elif force:
            atomic_write(target, text)
            forced.append(relpath)
        else:
            skipped.append(relpath)

    return InitReport(written=tuple(written), skipped=tuple(skipped), merged=tuple(merged), forced=tuple(forced))


def _merge_vocabulary_file(relpath: str, existing_text: str, generated_text: str) -> str:
    """Union-merge one vocabulary file so existing definitions win and new generated rows are appended."""
    filename = relpath.rsplit("/", maxsplit=1)[-1]

    existing_result = parse_vocabulary([(filename, existing_text)])
    generated_result = parse_vocabulary([(filename, generated_text)])

    stem = filename.removesuffix(".md")

    if stem == "entity-types":
        return _merge_entity_types(existing_result.index, generated_result.index, generated_text)
    if stem == "relation-types":
        return _merge_relation_types(existing_result.index, generated_result.index, generated_text)
    if stem == "facets":
        return _merge_facets(existing_result.index, generated_result.index, existing_text, generated_text)
    # Unknown vocabulary file, return the generated text unchanged
    return generated_text


def _merge_entity_types(existing_idx: VocabularyIndex, generated_idx: VocabularyIndex, generated_text: str) -> str:
    """Merge entity-type rows so existing definitions win and new keys are appended."""
    # Union: existing wins on conflict
    merged: dict[str, str] = dict(existing_idx.entity_types)
    for value, defn in generated_idx.entity_types.items():
        if value not in merged:
            merged[value] = defn

    # Re-render: replace table rows in the generated file with merged rows
    return _replace_table_rows_entity_types(generated_text, merged)


def _is_table_separator(line: str) -> bool:
    """Return True if `line` is a markdown table separator row (`|---|---|`)."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    # Remove outer pipes, then split on remaining | and check each cell is dashes/colons
    inner = stripped.strip("|")
    cells = inner.split("|")
    return len(cells) >= 1 and all(set(cell.strip()) <= {"-", ":"} and cell.strip() for cell in cells)


def _replace_table_rows_entity_types(text: str, merged: dict[str, str]) -> str:
    """Replace the data rows of the entity-types table in `text` with `merged`."""
    lines = text.splitlines()
    # Find the separator row (|---|---|---|)
    sep_idx = next((idx for idx, ln in enumerate(lines) if _is_table_separator(ln)), None)
    if sep_idx is None:
        return text  # not a table, return as-is

    # Find the end of the data rows (first non-table line after separator)
    end_idx = sep_idx + 1
    while end_idx < len(lines) and lines[end_idx].strip().startswith("|"):
        end_idx += 1

    new_rows = [f"| `{v}` | {d} | |" for v, d in merged.items()]
    result_lines = lines[: sep_idx + 1] + new_rows + lines[end_idx:]
    return "\n".join(result_lines)


def _merge_relation_types(existing_idx: VocabularyIndex, generated_idx: VocabularyIndex, generated_text: str) -> str:
    """Merge relation-type rows so existing definitions win and new keys are appended."""
    # Union: existing wins on conflict
    merged: dict[str, RelationSpec] = dict(existing_idx.relations)
    for slug, rel_spec in generated_idx.relations.items():
        if slug not in merged:
            merged[slug] = rel_spec

    # Re-render using merged data, split into normal + lifecycle
    return _replace_relation_table_rows(generated_text, merged)


def _replace_relation_table_rows(text: str, merged: dict[str, RelationSpec]) -> str:
    """Re-render the relation and lifecycle tables of `text` from `merged`."""
    # Separate normal from lifecycle
    normal: dict[str, RelationSpec] = {}
    lifecycle: dict[str, RelationSpec] = {}
    for slug, rel in merged.items():
        if not rel.domain and not rel.range:
            lifecycle[slug] = rel
        else:
            normal[slug] = rel

    # Re-render the two sections from scratch (simpler than patching in-place)
    lines = text.splitlines()

    # Find the frontmatter + H1 + intro (everything before first ##)
    h2_idx = next((idx for idx, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    header_lines = lines[:h2_idx]

    body_lines: list[str] = []
    if normal:
        body_lines += ["## Relations", "", "| Relation | Domain → Range | Meaning |", "|---|---|---|"]
        for slug, rel in normal.items():
            arrow = "↔" if rel.symmetric else "→"
            desc = rel.description.replace("|", "\\|")
            body_lines.append(f"| `{slug}` | {rel.domain} {arrow} {rel.range} | {desc} |")
        body_lines.append("")

    if lifecycle:
        body_lines += ["## Lifecycle", "", "| Relation | Meaning |", "|---|"]
        for slug, rel in lifecycle.items():
            desc = rel.description.replace("|", "\\|")
            body_lines.append(f"| `{slug}` | {desc} |")
        body_lines.append("")

    return "\n".join(header_lines + body_lines)


def _facet_sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each facet name to the `[start, end)` line span of the last H2 section headed by it."""
    starts = [idx for idx, line in enumerate(lines) if line.startswith("## ")]
    sections: dict[str, tuple[int, int]] = {}
    for position, start in enumerate(starts):
        match = _FACET_HEADING.match(lines[start])
        if match is not None:
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            sections[match.group("name").strip()] = (start, end)
    return sections


def _facet_values(index: VocabularyIndex, facet: str) -> Mapping[str, str]:
    """The values `index` registers for `facet`, the claim kinds for the `kind` facet."""
    return index.claim_types if facet == _CLAIM_KIND_FACET else index.facets.get(facet, {})


def _first_cell(row: str) -> str:
    """The first cell of the pipe-table `row`, an escaped pipe kept inside it."""
    return row.strip().replace("\\|", "\x00").strip("|").split("|")[0]


def _value_table_end(lines: list[str], start: int, end: int) -> int | None:
    """The line index just past the table whose row after a separator names a value in its first cell, or None when none does."""
    seen_separator = False
    for idx in range(start + 1, end):
        line = lines[idx].strip()
        if not line.startswith("|"):
            continue
        if _is_table_separator(line):
            seen_separator = True
        elif seen_separator and _BACKTICK_VALUE.search(_first_cell(line)):
            row_end = idx + 1
            while row_end < end and lines[row_end].strip().startswith("|"):
                row_end += 1
            return row_end
    return None


def _value_insertion(lines: list[str], span: tuple[int, int], values: Mapping[str, str]) -> tuple[int, list[str]]:
    """The line index and lines that add `values` to the section at `span`, as rows of its value table or an inline-code paragraph."""
    start, end = span
    row_end = _value_table_end(lines, start, end)
    if row_end is not None:
        rows: list[str] = []
        for value, definition in values.items():
            cell = value.replace("|", "\\|")
            escaped = definition.replace("|", "\\|")
            rows.append(f"| `{cell}` | {escaped} |")
        return row_end, rows
    content_end = end
    while content_end > start + 1 and not lines[content_end - 1].strip():
        content_end -= 1
    return content_end, ["", " · ".join(f"`{value}`" for value in values)]


def _merge_facets(existing_idx: VocabularyIndex, generated_idx: VocabularyIndex, existing_text: str, generated_text: str) -> str:
    """Add the generated facet values and sections the existing file lacks, leaving every other line of it as it was."""
    lines = existing_text.splitlines()
    existing_sections = _facet_sections(lines)
    generated_lines = generated_text.splitlines()

    insertions: list[tuple[int, list[str]]] = []
    appended: list[str] = []
    for facet, (start, end) in _facet_sections(generated_lines).items():
        span = existing_sections.get(facet)
        if span is None:
            section = generated_lines[start:end]
            while section and not section[-1].strip():
                section.pop()
            appended += ["", *section]
            continue
        known = _facet_values(existing_idx, facet)
        new_values = {value: definition for value, definition in _facet_values(generated_idx, facet).items() if value not in known}
        if new_values:
            insertions.append(_value_insertion(lines, span, new_values))

    if not insertions and not appended:
        return existing_text
    for index, new_lines in sorted(insertions, reverse=True):
        lines[index:index] = new_lines
    if appended:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += appended
    return "\n".join(lines) + "\n"
