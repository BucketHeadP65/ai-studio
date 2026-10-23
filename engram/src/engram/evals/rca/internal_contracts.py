"""The ports root-cause analysis depends on: the Logfire query client and the reflection model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from logfire.query_client import RowQueryResults

    from engram.evals.rca.domain.diagnosis import Diagnosis
    from engram.evals.rca.domain.reflection import DraftReflection


class SupportsLogfireQuery(Protocol):
    """The one method of a Logfire Query API client the report needs.

    Its return is logfire's `RowQueryResults`, a `{"columns", "rows"}` `TypedDict`.
    """

    def query_json_rows(self, sql: str, *, min_timestamp: datetime) -> RowQueryResults:
        """Run `sql` against the Logfire Query API and return the `{"columns", "rows"}` envelope.

        Args:
            sql: The SQL statement to run.
            min_timestamp: The earliest span time the query reads.

        Returns:
            The column names and the rows the query returned.
        """
        ...


class Reflector(Protocol):
    """Drafts a reflection from a diagnosis.

    The tuning loop holds a `Reflector | None` and drafts no findings on `None`.
    """

    async def reflect(self, diagnosis: Diagnosis) -> DraftReflection:
        """Draft a `DraftReflection` from `diagnosis`.

        Args:
            diagnosis: The deterministic diagnosis to draft against.

        Returns:
            The draft reflection, which `gate_reflection` must still re-validate.
        """
        ...


class ReflectorFactory(Protocol):
    """Builds the per-run `Reflector` for a model spec, or `None` when reflection is unconfigured.

    A `None` model spec yields no reflector, and the tuning loop then drafts no findings.
    """

    def __call__(self, model: str | None) -> Reflector | None:
        """Build a `Reflector` for `model`, or `None` when `model` is `None` (reflection off).

        Args:
            model: The model spec the reflector drafts with, or `None` to turn reflection off.

        Returns:
            The reflector for this run, or `None` when `model` is `None`.
        """
        ...
