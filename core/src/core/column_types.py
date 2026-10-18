"""SQLAlchemy column types for Postgres `LTREE` and for pgvector `halfvec` under asyncpg's binary codec.

`Ltree` carries a dotted label string such as `"s4.s1"` unchanged in both directions.
`HALFVEC` binds a dimension-checked `HalfVector`, so the asyncpg pgvector codec does the one encode.
Its read side is pgvector's own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pgvector import HalfVector
from pgvector.sqlalchemy import HALFVEC as _HALFVEC
from sqlalchemy.types import UserDefinedType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine.interfaces import Dialect
    from sqlalchemy.sql.type_api import _BindProcessorType, _ResultProcessorType

__all__ = ["HALFVEC", "Ltree"]


class HALFVEC(_HALFVEC):  # type: ignore[misc]  # pgvector's UserDefinedType is untyped, we narrow the bind path
    """The `halfvec(dim)` column type, bound as a `HalfVector` rather than as text.

    It differs from pgvector's `HALFVEC` only in `bind_processor`. A value of the wrong dimension is refused.
    """

    cache_ok = True

    def bind_processor(self, dialect: Dialect) -> _BindProcessorType[Sequence[float]]:
        """A processor that turns a list of floats, or a `HalfVector`, into a dimension-checked `HalfVector`.

        Args:
            dialect: The dialect in use, which is not read.

        Returns:
            The processor. It raises `ValueError` for a vector of the wrong dimension.
        """
        del dialect
        dim = self.dim

        def process(value: Sequence[float] | None) -> HalfVector | None:
            if value is None:
                return None
            vector = value if isinstance(value, HalfVector) else HalfVector(value)
            if dim is not None and vector.dimensions() != dim:
                msg = f"expected {dim} dimensions, not {vector.dimensions()}"
                raise ValueError(msg)
            return vector

        return process


def _passthrough(value: str | None) -> str | None:
    """An ltree value, unchanged."""
    return value


class Ltree(UserDefinedType[str]):
    """The Postgres `LTREE` column type, carried in Python as a dotted string."""

    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        """The DDL column type.

        Args:
            **_kw: Rendering options, which are not read.

        Returns:
            `LTREE`.
        """
        return "LTREE"

    def bind_processor(self, dialect: Dialect) -> _BindProcessorType[str]:
        """A processor that passes the dotted label string to the driver unchanged.

        Args:
            dialect: The dialect in use, which is not read.

        Returns:
            The processor.
        """
        del dialect
        return _passthrough

    def result_processor(self, dialect: Dialect, coltype: object) -> _ResultProcessorType[str]:
        """A processor that passes the dotted label string from the driver unchanged.

        Args:
            dialect: The dialect in use, which is not read.
            coltype: The driver's column type, which is not read.

        Returns:
            The processor.
        """
        del dialect, coltype
        return _passthrough
