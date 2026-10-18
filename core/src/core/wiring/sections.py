"""The settings sections: deriving them from a settings root, and registering each under its own type."""

from __future__ import annotations

from types import NoneType, UnionType
from typing import Union, get_args, get_origin

import wireup

from pydantic import BaseModel

from core.wiring.exceptions import SectionOptionalError, SectionsAmbiguousError


def section[T](value: T, *, as_type: type[T]) -> object:
    """A registration serving `value` under `as_type`.

    Args:
        value: The settings section this application was built with.
        as_type: The type consumers declare, which is the section model's own type.

    Returns:
        A registration to pass to the container, so a consumer declares `as_type` and never `as_type | None`.
    """
    return wireup.instance(value, as_type=as_type)


def section_type_of(annotation: object) -> type[BaseModel] | None:
    """The one model a field is registered under, or None when it names no model or more than one.

    A scalar is not a section, and neither is a `list[Model]` or a `dict[str, Model]`. `Model | None` names one model,
    and `sections_of` is what refuses it.

    Args:
        annotation: The annotation of a settings field.

    Returns:
        The one model the annotation names, or None.
    """
    models = _models_of(annotation)
    return models[0] if len(models) == 1 else None


def sections_of(settings: BaseModel) -> list[object]:
    """Every section the settings root declares, each under its own type, nested sections included.

    A field annotated as a model is a section, and so is every model-annotated field of that section, all the way
    down. Everything else is skipped.

    Args:
        settings: The settings root the application was built with.

    Returns:
        One registration per section, depth first, each parent before its nested sections.

    Raises:
        SectionsAmbiguousError: when a settings field names more than one model.
        SectionOptionalError: when a settings field allows None.
        LookupError: when the walk finds no model where the settings declare a section.
    """
    return [section(_value_at(settings, path), as_type=declared) for path, declared in _paths(type(settings), prefix="")]


def _models_of(annotation: object) -> tuple[type[BaseModel], ...]:
    """Every model an annotation allows, None left out."""
    origin = get_origin(annotation)
    if origin is not None and origin not in {Union, UnionType}:
        return ()
    candidates = tuple(argument for argument in get_args(annotation) if argument is not NoneType) or (annotation,)
    return tuple(candidate for candidate in candidates if isinstance(candidate, type) and issubclass(candidate, BaseModel))


def _paths(model: type[BaseModel], *, prefix: str) -> list[tuple[str, type[BaseModel]]]:
    """Each model-typed field of `model` as a `(path, type)` pair, its own sections after it."""
    found: list[tuple[str, type[BaseModel]]] = []
    for name, field in model.model_fields.items():
        models = _models_of(field.annotation)
        path = f"{prefix}{name}"
        if not models:
            continue
        if len(models) != 1:
            raise SectionsAmbiguousError(path, models)
        if NoneType in get_args(field.annotation):
            raise SectionOptionalError(path)
        found.append((path, models[0]))
        found.extend(_paths(models[0], prefix=f"{path}."))
    return found


def _value_at(settings: BaseModel, path: str) -> BaseModel:
    """The section `path` names, or a `LookupError` when the walk finds no model there."""
    found: object = settings
    for step in path.split("."):
        found = getattr(found, step, None)
    if isinstance(found, BaseModel):
        return found
    msg = f"The settings carry no {path} section."
    raise LookupError(msg)
