"""Builds the JSON Schema the `engram-init` skill ships as `knowledge-base-schema.schema.json`.

`KnowledgeBaseSchema.model_json_schema()` describes the model's own field shapes, but a knowledge_base_schema may
also be written in the looser forms the spec's before-validators accept. `knowledge_base_json_schema`
applies the one such widening the generator cannot express, so the shipped file accepts every
knowledge_base_schema the loader does. Run as a module to write the file:

```bash
uv run python -m engram.skills.engram_init_schema > src/engram/skills/engram-init/knowledge-base-schema.schema.json
```
"""

from __future__ import annotations

import json
import sys

from typing import TYPE_CHECKING

from engram.kb.contracts import KnowledgeBaseSchema

if TYPE_CHECKING:
    from pydantic import JsonValue

__all__ = ["knowledge_base_json_schema", "render_knowledge_base_json_schema"]

_BARE_SLUG_DESCRIPTION = "A bare slug string, or a CanonicalSection object declaring slug + heading + guidance."


def knowledge_base_json_schema() -> dict[str, JsonValue]:
    """`KnowledgeBaseSchema`'s JSON Schema, widened where a before-validator accepts more than the model shape.

    One widening: a page type's `canonical_sections` entry may be a bare slug string as well
    as a full `CanonicalSection` object (`CanonicalSection` folds the string into its own `slug`),
    which `model_json_schema()` renders as the object `$ref` alone.

    Returns:
        The schema mapping, ready to serialise.
    """
    schema: dict[str, JsonValue] = KnowledgeBaseSchema.model_json_schema()
    definitions = schema["$defs"]
    if not isinstance(definitions, dict):  # pragma: no cover (pydantic always emits a mapping here)
        return schema
    page_type = definitions["PageTypeSpec"]
    if not isinstance(page_type, dict):  # pragma: no cover (same)
        return schema
    properties = page_type["properties"]
    if not isinstance(properties, dict):  # pragma: no cover (same)
        return schema
    sections = properties["canonical_sections"]
    if not isinstance(sections, dict):  # pragma: no cover (same)
        return schema
    sections["items"] = {"anyOf": [{"type": "string"}, sections["items"]], "description": _BARE_SLUG_DESCRIPTION}
    return schema


def render_knowledge_base_json_schema() -> str:
    """The schema serialised exactly as the shipped `knowledge-base-schema.schema.json` holds it.

    Returns:
        The JSON text, indented by two spaces, with a trailing newline.
    """
    return json.dumps(knowledge_base_json_schema(), indent=2) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_knowledge_base_json_schema())
