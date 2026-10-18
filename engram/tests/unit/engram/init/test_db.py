"""The schema preflight: which way the database and the code disagree, and the fix each way needs."""

from __future__ import annotations

import pytest

from engram.init.contracts import SchemaAheadOfCodeError, SchemaBehindHeadError, SchemaRevisionError
from engram.init.domain.revision import revision_standing
from engram.init.infrastructure.db import _require_at_head

KNOWN = frozenset({"0032", "0033", "0034"})


def _known(revision: str) -> bool:
    return revision in KNOWN


class TestRevisionStanding:
    def test_a_known_older_revision_is_behind(self):
        assert revision_standing("0033", "0034", known=_known) == "behind"

    def test_a_never_migrated_database_is_behind(self):
        assert revision_standing(None, "0034", known=_known) == "behind"

    def test_the_head_is_at_head(self):
        assert revision_standing("0034", "0034", known=_known) == "at_head"

    def test_a_revision_this_code_does_not_know_is_ahead(self):
        assert revision_standing("0035", "0034", known=_known) == "ahead"


class TestRequireAtHead:
    def test_at_head_is_a_no_op(self):
        _require_at_head("0034", "0034", known=_known)

    def test_behind_names_both_revisions_and_the_upgrade(self):
        with pytest.raises(SchemaBehindHeadError) as exc_info:
            _require_at_head("0033", "0034", known=_known)

        message = str(exc_info.value)
        assert "0033" in message
        assert "0034" in message
        assert "alembic upgrade head" in message
        assert "studio init db" in message

    def test_a_never_migrated_database_reports_none_rather_than_a_python_none(self):
        with pytest.raises(SchemaBehindHeadError, match="none"):
            _require_at_head(None, "0034", known=_known)

    def test_ahead_names_both_revisions_and_a_newer_checkout_and_never_an_upgrade(self):
        with pytest.raises(SchemaAheadOfCodeError) as exc_info:
            _require_at_head("0035", "0034", known=_known)

        message = str(exc_info.value)
        assert "0035" in message
        assert "0034" in message
        assert "checkout" in message
        assert "upgrade" not in message
        assert "downgrade" not in message

    def test_both_errors_share_one_base_a_caller_can_catch(self):
        assert issubclass(SchemaBehindHeadError, SchemaRevisionError)
        assert issubclass(SchemaAheadOfCodeError, SchemaRevisionError)
