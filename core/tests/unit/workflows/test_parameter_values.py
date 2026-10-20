"""Admitting a run's arguments against the workflow's own parameter schemas.

The fixture is a decision brief: a question, the knowledge bases to read, the context and the
constraints it works under, plus a boolean, a number, an enum and an object. It is a shape to
validate against, and carries no product vocabulary of its own.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import (
    ArgumentRefusedError,
    FromParameter,
    InputPresentation,
    ParameterSchemaError,
    ParameterSpec,
    Workflow,
    normalize_arguments,
)
from core.ai.workflows.domain.parameter_values import schema_digest, validator_of

_BUDGET_SCHEMA = {
    "$ref": "#/$defs/Budget",
    "$defs": {
        "Budget": {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "currency": {"type": "string", "minLength": 1}},
            "required": ["amount", "currency"],
            "additionalProperties": False,
        }
    },
}
"""An object parameter addressed through a local definition, the one reference shape allowed here."""


def _brief() -> Workflow:
    """The decision brief's parameters: every JSON shape a person is asked for in one workflow."""
    return Workflow(
        name="decision-brief",
        version=1,
        description="Read the named knowledge bases and brief the person on one decision.",
        parameters={
            "question": ParameterSpec(description="the decision to take", json_schema={"type": "string", "minLength": 1}),
            "knowledge_bases": ParameterSpec(
                description="which knowledge bases to read",
                json_schema={
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                },
            ),
            "project_context": ParameterSpec(description="what this is about", json_schema={"type": "string"}, required=False),
            "constraints": ParameterSpec(description="what it must respect", json_schema={"type": "string"}, required=False),
            "include_rejected": ParameterSpec(description="keep the options ruled out", json_schema={"type": "boolean"}, required=False),
            "options_wanted": ParameterSpec(
                description="how many options", json_schema={"type": "integer", "minimum": 1, "maximum": 5}, required=False
            ),
            "horizon": ParameterSpec(
                description="when it is decided by", json_schema={"enum": ["this week", "this quarter", "this year"]}, required=False
            ),
            "budget": ParameterSpec(description="what it may cost", json_schema=_BUDGET_SCHEMA, required=False),
        },
        nodes=(),
        output=FromParameter(parameter="question"),
    )


def test_every_native_json_value_arrives_exactly_as_it_was_supplied() -> None:
    supplied = {
        "question": "Which cache?",
        "knowledge_bases": ["research", "repo-docs"],
        "project_context": "payments",
        "constraints": "offline",
        "include_rejected": True,
        "options_wanted": 3,
        "horizon": "this quarter",
        "budget": {"amount": 1200.5, "currency": "EUR"},
    }

    admitted = normalize_arguments(_brief(), supplied)

    assert admitted == supplied
    assert admitted["knowledge_bases"] == ["research", "repo-docs"], "an array stays an array"
    assert admitted["include_rejected"] is True, "a boolean stays a boolean"
    assert admitted["options_wanted"] == 3
    assert isinstance(admitted["budget"], dict)


def test_a_string_schema_keeps_a_string_that_looks_like_something_else() -> None:
    admitted = normalize_arguments(_brief(), {"question": "true", "knowledge_bases": ["research"]})

    assert admitted["question"] == "true", "a string schema never decodes its own value"
    assert isinstance(admitted["question"], str)


@pytest.mark.parametrize(
    ("parameter", "text", "decoded"),
    [
        ("include_rejected", "true", True),
        ("options_wanted", "4", 4),
        ("knowledge_bases", '["research"]', ["research"]),
        ("budget", '{"amount": 10, "currency": "EUR"}', {"amount": 10, "currency": "EUR"}),
    ],
)
def test_a_string_under_a_non_string_schema_decodes_once(parameter: str, text: str, decoded: object) -> None:
    supplied = {"question": "Which cache?", "knowledge_bases": ["research"], parameter: text}

    assert normalize_arguments(_brief(), supplied)[parameter] == decoded


def test_a_python_repr_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": "['research']"})

    assert "knowledge_bases" in str(refusal.value)


def test_a_blank_required_string_cannot_satisfy_min_length() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "", "knowledge_bases": ["research"]})

    assert refusal.value.parameter == "question"
    assert refusal.value.path == "$"
    assert refusal.value.detail, "the refusal says what the check found"


def test_too_many_knowledge_bases_are_refused_by_the_declared_ceiling() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["a", "b", "c", "d"]})

    assert refusal.value.parameter == "knowledge_bases"
    assert "maxItems" in str(refusal.value) or "too long" in str(refusal.value)


def test_a_nested_item_violating_its_own_constraint_is_refused_at_its_own_path() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research", ""]})

    assert refusal.value.parameter == "knowledge_bases"
    assert refusal.value.path == "$[1]", "the path names the item, not just the parameter"


def test_a_duplicate_is_refused_by_uniqueness() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research", "research"]})

    assert refusal.value.parameter == "knowledge_bases"
    assert "unique" in str(refusal.value)


def test_a_local_definition_reference_validates_the_object_it_names() -> None:
    admitted = normalize_arguments(
        _brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "budget": {"amount": 10, "currency": "EUR"}}
    )

    assert admitted["budget"] == {"amount": 10, "currency": "EUR"}


def test_a_malformed_object_is_refused_against_its_local_definition() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "budget": {"amount": 10}})

    assert refusal.value.parameter == "budget"


def test_a_value_outside_the_enum_is_refused() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "horizon": "next decade"})

    assert refusal.value.parameter == "horizon"


def test_a_native_boolean_is_not_an_integer() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "options_wanted": True})

    assert refusal.value.parameter == "options_wanted"


def test_a_native_number_is_not_a_boolean() -> None:
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "include_rejected": 1})

    assert refusal.value.parameter == "include_rejected"


def test_an_omitted_optional_string_is_filled_as_empty_and_every_other_kind_stays_absent() -> None:
    admitted = normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"]})

    assert admitted["project_context"] == ""
    assert admitted["constraints"] == ""
    assert "include_rejected" not in admitted
    assert "options_wanted" not in admitted
    assert "horizon" not in admitted
    assert "budget" not in admitted


def test_an_empty_string_is_a_value_rather_than_an_omission() -> None:
    admitted = normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "project_context": ""})

    assert admitted["project_context"] == ""


def test_normalizing_what_was_already_normalized_changes_nothing() -> None:
    supplied = {"question": "Which cache?", "knowledge_bases": ["research"], "include_rejected": False}

    once = normalize_arguments(_brief(), supplied)

    assert normalize_arguments(_brief(), once) == once


def test_an_argument_the_workflow_never_declared_travels_untouched() -> None:
    admitted = normalize_arguments(_brief(), {"question": "Which cache?", "knowledge_bases": ["research"], "leftover": "kept"})

    assert admitted["leftover"] == "kept"


def test_a_remote_reference_is_refused_when_the_parameter_is_validated() -> None:
    with pytest.raises(ValidationError):
        ParameterSpec(description="fetched from elsewhere", json_schema={"$ref": "https://example.test/schema.json"})


def test_a_remote_identifier_is_refused_when_the_parameter_is_validated() -> None:
    with pytest.raises(ValidationError):
        ParameterSpec(description="identified elsewhere", json_schema={"$id": "https://example.test/schema.json", "type": "string"})


def test_a_dynamic_reference_is_refused_when_the_parameter_is_validated() -> None:
    with pytest.raises(ValidationError):
        ParameterSpec(description="resolved at validation time", json_schema={"$dynamicRef": "#node"})


def test_a_schema_that_is_not_a_schema_is_refused_when_the_parameter_is_validated() -> None:
    with pytest.raises(ValidationError):
        ParameterSpec(description="not a schema", json_schema={"type": "nonsense"})


def test_a_format_is_an_annotation_rather_than_a_check() -> None:
    spec = Workflow(
        name="formatted",
        version=1,
        description="One string parameter carrying a format annotation.",
        parameters={"when": ParameterSpec(description="a date", json_schema={"type": "string", "format": "date"})},
        nodes=(),
        output=FromParameter(parameter="when"),
    )

    assert normalize_arguments(spec, {"when": "not a date"}) == {"when": "not a date"}


def test_a_reference_the_schema_does_not_carry_is_refused_when_the_parameter_is_validated() -> None:
    """A dangling local pointer passes `check_schema` and only fails while validating a value."""
    with pytest.raises(ValidationError):
        ParameterSpec(description="points at nothing", json_schema={"$ref": "#/$defs/Missing", "$defs": {}})


def test_an_anchor_the_schema_does_not_carry_is_refused_when_the_parameter_is_validated() -> None:
    with pytest.raises(ValidationError):
        ParameterSpec(description="anchored nowhere", json_schema={"$ref": "#absent", "$defs": {}})


def test_an_anchor_the_schema_does_carry_is_kept() -> None:
    spec = ParameterSpec(
        description="anchored in its own definitions",
        json_schema={"$ref": "#budget", "$defs": {"Budget": {"$anchor": "budget", "type": "object"}}},
    )

    assert spec.json_schema["$ref"] == "#budget"


def test_a_dangling_reference_that_reaches_admission_is_a_refusal_rather_than_a_crash() -> None:
    """A spec that got past validation some other way must still refuse, never raise a validator's own internals."""
    dangling = ParameterSpec.model_construct(description="points at nothing", json_schema={"$ref": "#/$defs/Missing", "$defs": {}})
    spec = Workflow.model_construct(
        name="dangling", version=1, description="A parameter pointing at a definition it never carried.", parameters={"budget": dangling}
    )

    with pytest.raises(ParameterSchemaError):
        normalize_arguments(spec, {"budget": {"amount": 1}})


def test_one_schema_is_compiled_once_however_its_keys_are_ordered() -> None:
    """Two spellings of one schema share a validator, so a run pays for compiling it once."""
    one = {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    other = {"maxItems": 3, "items": {"type": "string"}, "type": "array"}

    assert schema_digest(one) == schema_digest(other)
    assert validator_of(one) is validator_of(other)
    assert validator_of(dict(one)) is validator_of(one), "the same schema is never compiled twice"


def test_a_control_the_presentation_vocabulary_does_not_name_is_refused_at_parse() -> None:
    """The control set is closed, so no unknown selector can ever reach a launch's own admission.

    Asked through `model_validate`, because a stored card and a tool call are where an unknown
    control would actually arrive from.
    """
    with pytest.raises(ValidationError):
        InputPresentation.model_validate({"key": "knowledge_bases", "label": "Knowledge bases", "control": "whatever-selector"})


def test_every_named_control_parses_and_absent_means_absent() -> None:
    for control in ("text", "textarea", "knowledge-base", "json"):
        assert InputPresentation.model_validate({"key": "k", "label": "L", "control": control}).control == control
    assert InputPresentation(key="k", label="L").control is None
