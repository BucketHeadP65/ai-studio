from core.ai.workflows.contracts import Briefing, DocumentError, Program, load_step_document

TASK_DOC = """---
title: Digest one source
summary: One mirror becomes one digest.
version: 2
step:
  name: digest_source
  instructions: Read the whole mirror and write one digest page with quoted claims.
  inputs:
    source: {json_schema: {type: string}}
  output: {json_schema: {type: string}}
  tools: [fetch, write_note]
---

Use once per mirror. Do not use to update a digest.
"""

CODE_DOC = """---
title: Report
summary: Counts.
version: 1
step:
  name: report
  source: |
    {"n": len(items)}
  inputs:
    items: {json_schema: {type: array, items: {type: string}}}
  output: {json_schema: {type: object, properties: {n: {type: integer}}, required: [n]}}
---

Tail of a pass.
"""


def test_a_task_step_document_loads_its_step_and_applicability() -> None:
    doc = load_step_document(TASK_DOC)
    assert doc.title == "Digest one source"
    assert doc.version == 2
    assert isinstance(doc.step, Briefing)
    assert doc.step.name == "digest_source"
    assert doc.step.tools == ("fetch", "write_note")
    assert doc.applicability.startswith("Use once per mirror.")


def test_a_code_step_document_loads_as_a_program() -> None:
    doc = load_step_document(CODE_DOC)
    assert isinstance(doc.step, Program)
    assert doc.step.name == "report"


def test_a_document_without_a_step_key_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="step"):
        load_step_document("---\ntitle: x\nsummary: y\nversion: 1\n---\nbody\n")


def test_a_document_without_a_title_is_refused() -> None:
    import pytest

    with pytest.raises(DocumentError, match="title"):
        load_step_document("---\nsummary: y\nversion: 1\nstep: {name: x, source: '1', output: {json_schema: {type: integer}}}\n---\nbody\n")


def test_a_document_with_a_non_int_version_is_refused() -> None:
    import pytest

    with pytest.raises(DocumentError, match="version"):
        load_step_document(
            "---\ntitle: x\nsummary: y\nversion: true\nstep: {name: x, source: '1', output: {json_schema: {type: integer}}}\n---\nbody\n"
        )
