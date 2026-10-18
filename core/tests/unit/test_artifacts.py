"""Immutable value references: what one addresses, what canonical bytes are, and what is refused.

`core.ai.artifacts` is deliberately reachable without the workflow engine, so a consumer that only
resolves references never pays for a compiler it does not use.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from pydantic import JsonValue, ValidationError

from core.ai.artifacts import (
    MAX_CANONICAL_BYTES,
    ArtifactDocument,
    ArtifactMediaType,
    ArtifactRef,
    artifact_digest,
    canonical_bytes,
    text_media,
)

RUN = "20260915T120000000000-abcdef12"
DIGEST = "a" * 64


def _ref(**overrides: object) -> ArtifactRef:
    fields: dict[str, object] = {
        "id": "report",
        "label": "The quarterly report",
        "run_id": RUN,
        "node_id": "draft",
        "value_digest": DIGEST,
        "media_type": "application/json",
    }
    return ArtifactRef.model_validate(fields | overrides)


def test_the_artifact_module_is_reachable_without_the_workflow_engine() -> None:
    """A consumer resolving references imports no compiler, no registry and no validator."""
    probe = "import core.ai.artifacts, sys; print('core.ai.workflows' in sys.modules)"

    answered = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert answered.stdout.strip() == "False"


def test_the_workflow_package_re_exports_the_very_same_class() -> None:
    from core.ai.workflows.contracts import ArtifactRef as ReExported

    assert ReExported is ArtifactRef


def test_an_identifier_is_stripped_and_a_blank_one_is_refused() -> None:
    assert _ref(id="  report  ").id == "report"

    with pytest.raises(ValidationError, match="at least 1 character"):
        _ref(id="   ")


def test_an_identifier_past_its_bound_is_refused() -> None:
    with pytest.raises(ValidationError, match="at most 200 characters"):
        _ref(node_id="n" * 201)


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "a" * 65, f"sha256:{'a' * 64}", ""])
def test_a_digest_is_sixty_four_lowercase_hex_characters_and_nothing_else(bad: str) -> None:
    with pytest.raises(ValidationError):
        _ref(value_digest=bad)


def test_canonical_bytes_are_sorted_compact_and_unescaped() -> None:
    assert canonical_bytes({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'


def test_canonical_bytes_refuse_a_number_no_json_reader_can_hold() -> None:
    nonfinite: JsonValue = {"score": float("nan")}

    with pytest.raises(ValueError, match="not JSON compliant"):
        canonical_bytes(nonfinite)


def test_a_digest_is_the_sha256_of_exactly_those_bytes() -> None:
    import hashlib

    value: JsonValue = {"b": 1, "a": "é"}

    assert artifact_digest(value) == hashlib.sha256(canonical_bytes(value)).hexdigest()


def test_the_canonical_size_ceiling_is_a_million_bytes() -> None:
    assert MAX_CANONICAL_BYTES == 1_000_000


def test_a_value_whose_canonical_bytes_pass_the_ceiling_is_no_document() -> None:
    oversize = "x" * (MAX_CANONICAL_BYTES + 1)
    ref = _ref(media_type="text/plain", value_digest=artifact_digest(oversize))

    with pytest.raises(ValidationError, match="1000000"):
        ArtifactDocument(ref=ref, value=oversize)


def test_a_value_just_under_the_ceiling_is_a_document() -> None:
    fits = "x" * (MAX_CANONICAL_BYTES - 2)  # the two quotes are canonical bytes too
    ref = _ref(media_type="text/plain", value_digest=artifact_digest(fits))

    assert ArtifactDocument(ref=ref, value=fits).value == fits


@pytest.mark.parametrize("media", ["text/markdown", "text/plain"])
def test_text_media_carries_a_string_and_refuses_anything_else(media: ArtifactMediaType) -> None:
    value: JsonValue = {"text": "hello"}
    ref = _ref(media_type=media, value_digest=artifact_digest(value))

    assert text_media(media) is True
    with pytest.raises(ValidationError, match="string"):
        ArtifactDocument(ref=ref, value=value)


def test_json_media_carries_whatever_json_covers() -> None:
    value: JsonValue = {"rows": [1, 2, 3]}
    document = ArtifactDocument(ref=_ref(value_digest=artifact_digest(value)), value=value)

    assert document.value == value
    assert text_media("application/json") is False


def test_a_document_whose_value_is_not_what_its_reference_addresses_is_refused() -> None:
    with pytest.raises(ValidationError, match="digest"):
        ArtifactDocument(ref=_ref(), value={"anything": "else"})


def test_a_document_refuses_a_field_nobody_declared() -> None:
    value: JsonValue = {"rows": []}
    payload = {"ref": _ref(value_digest=artifact_digest(value)).model_dump(), "value": value, "url": "https://example.test"}

    with pytest.raises(ValidationError, match="Extra inputs"):
        ArtifactDocument.model_validate(payload)


def test_a_reference_refuses_a_field_nobody_declared() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        ArtifactRef.model_validate({**_ref().model_dump(), "url": "https://example.test"})


def test_a_reference_is_frozen_once_minted() -> None:
    ref = _ref()

    with pytest.raises(ValidationError, match="frozen"):
        ref.id = "other"  # type: ignore[misc]


def test_a_turn_is_optional_and_stripped_when_given() -> None:
    assert _ref().turn is None
    assert _ref(turn=" draft ").turn == "draft"


def test_a_document_round_trips_through_json_unchanged() -> None:
    value: JsonValue = {"a": [1, {"b": None}], "c": True}
    document = ArtifactDocument(ref=_ref(value_digest=artifact_digest(value)), value=value)

    assert ArtifactDocument.model_validate(json.loads(document.model_dump_json())) == document
