"""The logging facade: message templates, named fields, exceptions and spans."""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from core.observability import configure_logging, get_logger, span

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

NAME = "core.observability.test"


def _only(records: Sequence[logging.LogRecord]) -> logging.LogRecord:
    mine = [record for record in records if record.name == NAME]
    assert len(mine) == 1
    return mine[0]


def test_a_template_reads_with_its_fields_filled_in(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("indexed {pages} pages into {collection}", pages=12, collection="notes")

    record = _only(caplog.records)
    assert record.getMessage() == "indexed 12 pages into notes"
    assert record.__dict__["logfire.msg_template"] == "indexed {pages} pages into {collection}"


def test_every_field_is_kept_as_its_own_attribute(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("indexed", pages=12, ids=["a", "b"], ratio=0.5, done=True)

    record = _only(caplog.records)
    assert (record.__dict__["pages"], record.__dict__["ids"], record.__dict__["ratio"], record.__dict__["done"]) == (12, ("a", "b"), 0.5, True)


def test_a_field_left_none_is_dropped_and_its_placeholder_stays(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("read {path}", path=None)

    record = _only(caplog.records)
    assert record.getMessage() == "read {path}"
    assert "path" not in record.__dict__


def test_a_field_named_like_a_record_attribute_is_kept_under_a_trailing_underscore(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("opened {name}", name="repo-docs")

    record = _only(caplog.records)
    assert record.name == NAME
    assert record.__dict__["name_"] == "repo-docs"
    assert record.getMessage() == "opened repo-docs"


def test_a_mixed_sequence_is_kept_as_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("mixed", values=["a", 1])  # pyright: ignore[reportArgumentType] (a mixed sequence is what is under test)

    assert _only(caplog.records).__dict__["values"] == "['a', 1]"


def _fail() -> None:
    msg = "boom"
    raise ValueError(msg)


def test_exception_records_the_exception_being_handled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=NAME):
        try:
            _fail()
        except ValueError:
            get_logger(NAME).exception("the step failed")

    record = _only(caplog.records)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], ValueError)


def test_a_warning_carries_the_exception_it_is_given(caplog: pytest.LogCaptureFixture) -> None:
    caught = OSError("disk full")
    with caplog.at_level(logging.WARNING, logger=NAME):
        get_logger(NAME).warning("kept the last snapshot", error=caught)

    record = _only(caplog.records)
    assert record.exc_info is not None
    assert record.exc_info[1] is caught


def test_the_record_points_at_the_line_that_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("here")

    assert _only(caplog.records).funcName == "test_the_record_points_at_the_line_that_logged"


def test_a_level_below_the_threshold_records_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=NAME):
        get_logger(NAME).debug("noise {n}", n=1)

    assert [record for record in caplog.records if record.name == NAME] == []


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()


def test_span_records_attributes_without_a_configuration_warning(recwarn: pytest.WarningsRecorder) -> None:
    with span("indexing {collection}", collection="notes", skipped=None) as active:
        active.set_attribute("discovered", "mid-operation")

    assert not recwarn.list  # in particular, no "Logfire not configured" warning


def test_a_template_may_ask_for_the_repr_or_a_format(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=NAME):
        get_logger(NAME).info("gate {gate!r} at {share:.0%}", gate="g1", share=0.5)

    assert _only(caplog.records).getMessage() == "gate 'g1' at 50%"
