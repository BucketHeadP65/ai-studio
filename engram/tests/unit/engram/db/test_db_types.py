"""Unit tests for the custom SQLAlchemy types in `core.column_types`.

`Ltree` is a thin `UserDefinedType` that renders the Postgres `LTREE`
column type and passes `str` values through unchanged (the heavy lifting is
done server-side by the ltree extension). `HALFVEC` subclasses
`pgvector.sqlalchemy.HALFVEC` but rebinds `bind_processor` to hand the driver
a :class:`pgvector.HalfVector` (not pgvector's text form): with the asyncpg
binary codec registered (`orm.register_pgvector`) the driver encodes that value
itself, so the ORM never double-converts a vector into a string the codec then
re-parses character-by-character.
"""

from __future__ import annotations

import pgvector.sqlalchemy
import pytest

from core.column_types import HALFVEC, Ltree
from pgvector import HalfVector
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.types import UserDefinedType

_DIALECT = PGDialect_asyncpg()


class TestLtree:
    def test_is_a_user_defined_type(self):
        assert issubclass(Ltree, UserDefinedType)

    def test_col_spec_is_ltree(self):
        assert Ltree().get_col_spec() == "LTREE"

    def test_bind_processor_passes_str_through(self):
        processor = Ltree().bind_processor(_DIALECT)

        assert processor("s4.s1") == "s4.s1"

    def test_result_processor_passes_str_through(self):
        processor = Ltree().result_processor(_DIALECT, coltype=None)

        assert processor("s4.s1") == "s4.s1"


class TestHalfvec:
    def test_subclasses_pgvector_halfvec(self):
        assert issubclass(HALFVEC, pgvector.sqlalchemy.HALFVEC)

    def test_col_spec_carries_the_dimension(self):
        assert HALFVEC(1024).get_col_spec() == "HALFVEC(1024)"

    def test_bind_processor_returns_a_half_vector_not_a_string(self):
        # The asyncpg binary codec encodes a HalfVector itself; pgvector's stock
        # bind_processor instead returns the text form `"[a,b,c]"`, which the codec
        # then mis-parses char-by-char ("could not convert string to float"). Ours
        # must hand the driver a HalfVector for the codec to binary-encode.
        processor = HALFVEC(3).bind_processor(_DIALECT)

        bound = processor([1.0, 2.0, 3.0])

        assert isinstance(bound, HalfVector)
        assert [float(x) for x in bound.to_list()] == [1.0, 2.0, 3.0]

    def test_bind_processor_passes_none_through(self):
        processor = HALFVEC(3).bind_processor(_DIALECT)

        assert processor(None) is None

    def test_bind_processor_rejects_a_wrong_dimension(self):
        processor = HALFVEC(3).bind_processor(_DIALECT)

        with pytest.raises(ValueError, match="3"):
            processor([1.0, 2.0])

    def test_result_processor_reads_a_half_vector_as_a_list(self):
        # asyncpg's codec already decodes `halfvec` to a HalfVector; the result
        # processor accepts that and returns its plain list[float] form.
        vector = HalfVector([1.0, 2.0, 3.0])
        processor = HALFVEC(3).result_processor(_DIALECT, coltype=None)

        assert processor(vector) == [1.0, 2.0, 3.0]
