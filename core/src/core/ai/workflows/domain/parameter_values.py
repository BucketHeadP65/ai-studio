"""Admitting a run's arguments against the workflow's own parameter declarations.

A parameter says what it accepts as a JSON Schema, so that declaration is what a supplied value is
checked against, keyword for keyword: bounds, item constraints, uniqueness, combinators and local
definitions all hold. A JSON value is admitted exactly as it arrives and is never converted into
something else. The one conversion here is for a caller that can send nothing but text, and it is a
decode of that text, never a coercion of a value that already has a type.

A parameter's schema is checked once, when the parameter itself is validated, and every schema that
would have to be fetched from somewhere is refused there. Validation therefore reads nothing but the
schema in front of it.
"""

from __future__ import annotations

import json

from hashlib import sha256
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from pydantic import JsonValue
from referencing.exceptions import Unresolvable

from core.ai.workflows.exceptions import ArgumentRefusedError, ParameterSchemaError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.domain.vocabulary import Workflow

WorkflowArguments = dict[str, JsonValue]
"""What a run is given: one JSON value per parameter name, the shape every launch path carries.

Each value has the JSON type its parameter declares, and text is only one of those types."""

_LOCAL = "#"
"""The one prefix a reference inside a parameter's own schema may start with."""


_checked: set[str] = set()
"""Digests of the schemas already found usable, so one schema is checked once per process."""

_validators: dict[str, Draft202012Validator] = {}
"""One validator per distinct schema, keyed by the same digest."""


def schema_digest(schema: Mapping[str, object]) -> str:
    """A schema's content address: the same schema written two ways gets the same key.

    Args:
        schema: A parameter's JSON Schema.

    Returns:
        The SHA-256 of the schema's canonical JSON, as lowercase hex.

    Raises:
        ParameterSchemaError: the schema holds something JSON cannot carry.
    """
    try:
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        msg = f"a parameter's schema must be JSON: {exc}"
        raise ParameterSchemaError(msg) from exc
    return sha256(canonical.encode("utf-8")).hexdigest()


def _pointed_at(root: Mapping[str, object], pointer: str) -> Mapping[str, object] | None:
    """The subschema one local JSON pointer names, or `None` when this schema does not carry it."""
    found: object = root
    for raw in pointer.removeprefix(f"{_LOCAL}/").split("/"):
        step = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(found, dict) or step not in found:
            return None
        found = found[step]
    return found if isinstance(found, dict) else None


def _anchors_of(node: object, found: set[str]) -> None:
    """Collect every `$anchor` the schema declares, so a `#name` reference can be checked against them."""
    if isinstance(node, list):
        for item in node:
            _anchors_of(item, found)
        return
    if not isinstance(node, dict):
        return
    anchor = node.get("$anchor")
    if isinstance(anchor, str):
        found.add(anchor)
    for value in node.values():
        _anchors_of(value, found)


def _refuse_a_reference_nothing_answers(reference: str, root: Mapping[str, object], anchors: set[str]) -> None:
    """Refuse a local reference the schema does not actually carry."""
    if reference == _LOCAL:
        return
    if reference.startswith(f"{_LOCAL}/"):
        if _pointed_at(root, reference) is None:
            msg = f"a parameter's schema points at {reference!r}, which it does not carry"
            raise ParameterSchemaError(msg)
        return
    if reference.removeprefix(_LOCAL) not in anchors:
        msg = f"a parameter's schema points at {reference!r}, and no $anchor of that name is declared in it"
        raise ParameterSchemaError(msg)


def _refuse_what_nothing_here_answers(node: object, root: Mapping[str, object], anchors: set[str]) -> None:
    """Walk one schema and refuse every reference this schema alone cannot answer."""
    if isinstance(node, list):
        for item in node:
            _refuse_what_nothing_here_answers(item, root, anchors)
        return
    if not isinstance(node, dict):
        return
    for keyword, value in node.items():
        if keyword == "$dynamicRef":
            msg = "a parameter's schema may not use $dynamicRef: it resolves against whatever is loaded, which is nothing here"
            raise ParameterSchemaError(msg)
        if keyword in ("$ref", "$id") and isinstance(value, str):
            if not value.startswith(_LOCAL):
                msg = f"a parameter's schema may not use {keyword} {value!r}: only a reference inside the schema itself can be resolved"
                raise ParameterSchemaError(msg)
            if keyword == "$ref":
                _refuse_a_reference_nothing_answers(value, root, anchors)
        _refuse_what_nothing_here_answers(value, root, anchors)


def check_parameter_schema(schema: Mapping[str, object]) -> None:
    """Refuse a parameter declaration no value could honestly be admitted against.

    It checks first that nothing in the schema reaches outside itself, then that it is a valid Draft
    2020-12 schema. A remote `$id`, a `$ref` that is not a fragment of this schema, any `$dynamicRef`,
    and a fragment that names nothing in the schema are all refused. A schema that passes is not
    checked again in this process.

    Args:
        schema: A parameter's JSON Schema.

    Raises:
        ParameterSchemaError: the schema reaches outside itself, or is not a schema.
    """
    digest = schema_digest(schema)
    if digest in _checked:
        return
    anchors: set[str] = set()
    _anchors_of(schema, anchors)
    _refuse_what_nothing_here_answers(schema, schema, anchors)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        msg = f"a parameter's schema must be a JSON Schema: {exc.message}"
        raise ParameterSchemaError(msg) from exc
    _checked.add(digest)


def validator_of(schema: Mapping[str, object]) -> Draft202012Validator:
    """The validator for one schema, built once and kept by content address.

    Two spellings of one schema share one validator for the life of the process.

    Args:
        schema: A parameter's JSON Schema.

    Returns:
        The Draft 2020-12 validator for it.

    Raises:
        ParameterSchemaError: the schema holds something JSON cannot carry.
    """
    digest = schema_digest(schema)
    validator = _validators.get(digest)
    if validator is None:
        validator = Draft202012Validator(schema)
        _validators[digest] = validator
    return validator


def _accepts_a_string(schema: Mapping[str, object], root: Mapping[str, object], seen: frozenset[str]) -> bool:
    """Whether `schema` leaves room for a string, following its local references and combinators."""
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared == "string"
    if isinstance(declared, list):
        return "string" in declared
    if "const" in schema:
        return isinstance(schema["const"], str)
    choices = schema.get("enum")
    if isinstance(choices, list):
        return any(isinstance(choice, str) for choice in choices)
    return _accepts_a_string_indirectly(schema, root, seen)


def _accepts_a_string_indirectly(schema: Mapping[str, object], root: Mapping[str, object], seen: frozenset[str]) -> bool:
    """The same question for a schema that names no type of its own: through its reference, or its branches."""
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference not in seen:
        named = _pointed_at(root, reference)
        # An unresolvable reference reads as accepting a string: supplied text is then kept as it
        # was given and refused by the validator, rather than decoded on a guess.
        return True if named is None else _accepts_a_string(named, root, seen | {reference})
    every = schema.get("allOf")
    if isinstance(every, list):
        return all(not isinstance(branch, dict) or _accepts_a_string(branch, root, seen) for branch in every)
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            return any(isinstance(branch, dict) and _accepts_a_string(branch, root, seen) for branch in branches)
    return True


def _decoded(parameter: str, schema: Mapping[str, object], value: JsonValue) -> JsonValue:
    """`value` as the JSON its parameter declares, decoding supplied text once where text cannot fit."""
    if not isinstance(value, str) or _accepts_a_string(schema, schema, frozenset()):
        return value
    try:
        read: JsonValue = json.loads(value)
    except ValueError as exc:
        # The refusal names where the text stopped being JSON and never repeats the text itself.
        msg = f"the text supplied is not the JSON this parameter declares ({exc})"
        raise ArgumentRefusedError(parameter, "$", msg) from exc
    return read


def _admitted(parameter: str, schema: Mapping[str, object], value: JsonValue) -> JsonValue:
    """One supplied value, checked against its parameter's own schema."""
    check_parameter_schema(schema)
    decoded = _decoded(parameter, schema, value)
    try:
        failure = best_match(validator_of(schema).iter_errors(decoded))
    except Unresolvable as exc:
        # `check_parameter_schema` refuses every reference this schema cannot answer, so nothing
        # should reach here. Without this, a validator's own internal error escapes a launch as a
        # crash rather than as a refusal.
        msg = f"a parameter's schema could not be resolved: {exc}"
        raise ParameterSchemaError(msg) from exc
    if failure is not None:
        raise ArgumentRefusedError(parameter, failure.json_path, failure.message)
    return decoded


def normalize_arguments(workflow: Workflow, supplied: Mapping[str, JsonValue]) -> WorkflowArguments:
    """What `workflow` is run with, given what a caller supplied. The one admission every launch shares.

    Each declared parameter's value is checked against that parameter's own schema and kept as it
    is. Supplied text under a parameter that cannot hold text is read as JSON once and then checked
    like any other value, so a caller with nothing but strings (a stored schedule, a form) can reach
    a parameter that declares an array, a number or a boolean.
    Nothing else is converted: a string parameter keeps the string it was given, whatever the text
    looks like, and a value of the wrong JSON type is refused rather than made to fit.

    An argument naming no declared parameter travels untouched: this admits values, it does not
    decide which arguments a workflow may be sent. A declared parameter nobody supplied stays
    absent, except an optional string one, which arrives empty so the wire reading it resolves.

    Args:
        workflow: The workflow being run, whose parameters carry the schemas.
        supplied: What the caller gave, by parameter name.

    Returns:
        A new mapping. The caller's own is left alone.

    Raises:
        ArgumentRefusedError: one supplied value its parameter's schema will not admit.
        ParameterSchemaError: one parameter's schema is not one, or reaches outside itself.
    """
    admitted: WorkflowArguments = {
        name: value if (parameter := workflow.parameters.get(name)) is None else _admitted(name, parameter.json_schema, value)
        for name, value in supplied.items()
    }
    for name, parameter in workflow.parameters.items():
        if not parameter.required and name not in admitted and parameter.json_schema.get("type") == "string":
            admitted[name] = ""
    return admitted
