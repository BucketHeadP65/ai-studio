"""Workflow files and step files: markdown documents carrying a workflow or a reusable step in their frontmatter.

A workflow file read validates the workflow under one key and returns the rest of the frontmatter
untouched for whoever owns those fields. Beyond the workflow it checks the format only: a closed
frontmatter block, valid YAML, and no key that YAML turned into a non-string.

A step file read needs a closed frontmatter block of valid YAML with a `title`, a `summary`, a
`version` and the step under one key. The body becomes the step's applicability, and every other
key is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from pydantic import BaseModel, ConfigDict, PositiveInt, TypeAdapter

from core.ai.workflows.domain.vocabulary import InlineStep, Workflow
from core.ai.workflows.exceptions import DocumentError

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_SPEC_KEY = "workflow"
"""The frontmatter key a workflow sits under when the caller names no other."""


@dataclass(frozen=True)
class SpecDocument:
    """One parsed workflow file: the validated workflow, the rest of its frontmatter, and the body.

    `frontmatter` is a mapping this module never interprets. The library that wrote the document
    owns what its fields mean.
    """

    workflow: Workflow
    frontmatter: Mapping[str, object]
    body: str


def load_spec_document(text: str, *, key: str = DEFAULT_SPEC_KEY) -> SpecDocument:
    """Parse a workflow file and validate the workflow under `key`.

    Args:
        text: The document, frontmatter first.
        key: The frontmatter key the workflow sits under.

    Returns:
        The workflow, the rest of the frontmatter, and the markdown body.

    Raises:
        DocumentError: If the frontmatter is missing, unclosed, not YAML, has coerced
            keys, or carries no workflow under `key`.
        pydantic.ValidationError: If the workflow itself is malformed. It is raised unwrapped,
            with its field-level errors.
    """
    raw, body = _split_frontmatter(text)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"frontmatter is not valid YAML: {exc}"
        raise DocumentError(msg) from exc
    if not isinstance(data, dict):
        msg = f"frontmatter must be a mapping, got {type(data).__name__}"
        raise DocumentError(msg)
    _reject_coerced_keys(data)
    if key not in data:
        available = ", ".join(sorted(str(k) for k in data)) or "(none)"
        msg = f"no spec under frontmatter key {key!r}. Keys present: {available}"
        raise DocumentError(msg)
    workflow = Workflow.model_validate(data[key])
    frontmatter = {k: v for k, v in data.items() if k != key}
    return SpecDocument(workflow=workflow, frontmatter=frontmatter, body=body)


def dump_spec_document(
    workflow: Workflow, *, frontmatter: Mapping[str, object] | None = None, body: str = "", key: str = DEFAULT_SPEC_KEY
) -> str:
    """Render a workflow as a workflow file, beside whatever frontmatter the caller owns.

    Args:
        workflow: The workflow to write.
        frontmatter: The caller's own frontmatter fields. A field under `key` is replaced by the workflow.
        body: The markdown after the frontmatter, if any.
        key: The frontmatter key the workflow is written under.

    Returns:
        The document text, frontmatter first.
    """
    data: dict[str, object] = dict(frontmatter or {})
    data[key] = workflow.model_dump(mode="json")
    rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)
    text = f"---\n{rendered}---\n"
    if body:
        text += f"\n{body}"
    return text


def _reject_coerced_keys(data: object, path: str = "") -> None:
    """Refuse frontmatter with a key YAML 1.1 turned into a non-string, such as a bare `on` read as True."""
    if isinstance(data, dict):
        for key, value in data.items():
            if not isinstance(key, str):
                where = path or "frontmatter"
                msg = (
                    f"{where} has a non-string key {key!r}: YAML 1.1 reads bare on/off/yes/no/true/null as "
                    f"scalars, so the key was coerced and its field is now missing. Quote the key, or use the "
                    f"vocabulary's own field names (a gate reviews with `reviews:`, not `on:`)."
                )
                raise DocumentError(msg)
            _reject_coerced_keys(value, f"{path}.{key}" if path else key)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            _reject_coerced_keys(item, f"{path}[{index}]")


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        msg = "document has no frontmatter: expected the text to start with `---`"
        raise DocumentError(msg)
    end = text.find("\n---\n", 4)
    if end == -1:
        msg = "document frontmatter is not closed: no terminating `---` line"
        raise DocumentError(msg)
    body = text[end + 5 :]
    return text[4 : end + 1], body.removeprefix("\n")


_STEP = TypeAdapter(InlineStep)
"""Validates a reusable step's frontmatter back into its concrete `Briefing | Program | AgentStep`."""


class StepDocument(BaseModel):
    """A reusable step as a file: its card, its version, and the step itself."""

    model_config = ConfigDict(frozen=True)

    title: str
    summary: str
    applicability: str
    version: PositiveInt
    step: InlineStep


def load_step_document(text: str, *, key: str = "step") -> StepDocument:
    """Parse a step file whose frontmatter carries a reusable step under `key`. The body is its applicability text.

    Args:
        text: The document, frontmatter first.
        key: The frontmatter key the step sits under.

    Returns:
        The step with its title, summary, version and applicability.

    Raises:
        DocumentError: When the frontmatter is missing, unclosed, not YAML or not a mapping, or
            when `title`, `summary`, `version` or `key` is missing or of the wrong type.
        pydantic.ValidationError: When the step itself does not parse.
    """
    raw, body = _split_frontmatter(text)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        msg = f"frontmatter is not valid YAML: {exc}"
        raise DocumentError(msg) from exc
    if not isinstance(data, dict):
        msg = f"frontmatter must be a mapping, got {type(data).__name__}"
        raise DocumentError(msg)
    title = _required_field(data, "title", str)
    summary = _required_field(data, "summary", str)
    version = _required_field(data, "version", int)
    if key not in data:
        msg = f"no {key!r} in the document's frontmatter"
        raise DocumentError(msg)
    return StepDocument(
        title=title,
        summary=summary,
        applicability=body.strip(),
        version=version,
        step=_STEP.validate_python(data[key]),
    )


def _required_field[T](data: Mapping[str, object], key: str, kind: type[T]) -> T:
    """One frontmatter field, present and of type `kind` (a bool never passes as an int), or a `DocumentError` naming `key`."""
    if key not in data:
        msg = f"no {key!r} in the document's frontmatter"
        raise DocumentError(msg)
    value = data[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        msg = f"{key!r} in the document's frontmatter must be a {kind.__name__}, got {type(value).__name__}"
        raise DocumentError(msg)
    return value
