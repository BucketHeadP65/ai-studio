"""The step lock file beside a `steps/` folder: each step file's version and content address, as last recorded.

`harness steps lock <folder>` writes it.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import DocumentError, InlineStep, load_step_document, step_content_address
from core.files import atomic_write
from pydantic import BaseModel, ConfigDict, JsonValue, PositiveInt, TypeAdapter, ValidationError

from harness.features.browsing.exceptions import StepLockRefusedError

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepDocument

LOCK_FILE = "steps.lock"
"""The step lock file's name, beside the `steps/` folder it describes."""

_STEP = TypeAdapter(InlineStep)
"""Validates a reusable step as the library reads it back."""


class StepLockEntry(BaseModel):
    """One step file as the lock records it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: PositiveInt
    """The version the step file declares."""
    step_hash: str
    """The step's content address."""


class StepLock(BaseModel):
    """Every step file of one `steps/` folder, by step name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: dict[str, StepLockEntry]
    """Each step's entry, under the step's name."""


def read_step_file(file: Path) -> StepDocument:
    """Parse one step file, refusing a step the library would read back with another content address.

    The library drops null values and empty lists and mappings at every depth when it stores a step, so a step
    holding one in a field it sets is refused, naming each such value.

    Args:
        file: The step file.

    Returns:
        The step with its title, summary, version and applicability.

    Raises:
        DocumentError: The file does not parse as a step file, or its step would read back different. The message
            names the file.
    """
    try:
        document = load_step_document(file.read_text(encoding="utf-8"))
    except DocumentError as error:
        msg = f"{file}: {error}"
        raise DocumentError(msg) from error
    _refuse_stored_differently(file, document.step)
    return document


def _refuse_stored_differently(file: Path, step: InlineStep) -> None:
    """Refuse a step the library would read back with another content address, naming the values it would drop."""
    try:
        same = step_content_address(_STEP.validate_python(_stored(step.model_dump(mode="json")))) == step_content_address(step)
    except ValidationError:
        same = False
    if same:
        return
    fields = ", ".join(f"`{field}`" for field in _emptied(step.model_dump(mode="json", exclude_defaults=True))) or "each null value and empty list or mapping"
    msg = (
        f"{file}: the library drops null values and empty lists and mappings when it stores a step, so this step would "
        f"read back different. Give {fields} a value or remove it."
    )
    raise DocumentError(msg)


def _stored(value: JsonValue) -> JsonValue:
    """`value` as the library stores it: null values and empty lists and mappings dropped at every depth, else None."""
    if isinstance(value, list):
        items: list[JsonValue] = [kept for kept in (_stored(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, dict):
        nested: dict[str, JsonValue] = {key: kept for key, kept in ((key, _stored(item)) for key, item in value.items()) if kept is not None}
        return nested or None
    return value


def _emptied(value: JsonValue, at: str = "") -> list[str]:
    """The dotted path of every null value and empty list or mapping inside `value`."""
    if value is None or (isinstance(value, list | dict) and not value):
        return [at]
    if isinstance(value, list):
        return [found for index, item in enumerate(value) for found in _emptied(item, f"{at}.{index}" if at else str(index))]
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in _emptied(item, f"{at}.{key}" if at else key)]
    return []


def read_lock(folder: Path) -> StepLock:
    """The step lock file in `folder`, or an empty lock when the folder has none yet.

    Args:
        folder: The folder holding `steps/` and `steps.lock`.

    Returns:
        The lock as the file records it.

    Raises:
        pydantic.ValidationError: The lock file is not a step lock.
    """
    path = folder / LOCK_FILE
    if not path.is_file():
        return StepLock(steps={})
    return StepLock.model_validate_json(path.read_text(encoding="utf-8"))


def locked_steps(folder: Path) -> StepLock:
    """The lock the step files in `folder/steps` call for now: each step's declared version and content address.

    Args:
        folder: The folder holding `steps/`.

    Returns:
        The lock, computed from the files.

    Raises:
        DocumentError: A step file does not parse. The message names it.
    """
    documents = [read_step_file(file) for file in sorted((folder / "steps").glob("*.md"))]
    return StepLock(
        steps={
            document.step.name: StepLockEntry(version=document.version, step_hash=step_content_address(document.step))
            for document in sorted(documents, key=lambda document: document.step.name)
        }
    )


def write_lock(folder: Path) -> StepLock:
    """Record every step file in `folder/steps` in `folder/steps.lock`, refusing a step changed at an unchanged version.

    A step whose content address differs from its entry must declare a version above the entry's. A new step file is
    recorded as it is, and a step file that is gone leaves the lock. A refusal writes nothing.

    Args:
        folder: The folder holding `steps/`.

    Returns:
        The lock written.

    Raises:
        StepLockRefusedError: A step changed while its version did not rise. It names each such step.
        DocumentError: A step file does not parse. The message names it.
    """
    held = read_lock(folder).steps
    lock = locked_steps(folder)
    changed = {
        name: entry.version
        for name, entry in lock.steps.items()
        if (prior := held.get(name)) is not None and prior.step_hash != entry.step_hash and entry.version <= prior.version
    }
    if changed:
        raise StepLockRefusedError(folder, changed)
    atomic_write(folder / LOCK_FILE, json.dumps(lock.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
    return lock
