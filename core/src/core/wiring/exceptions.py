"""The errors the wiring helpers raise when a settings root cannot be split into sections."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


class SectionsAmbiguousError(Exception):
    """A settings field names more than one model, so no single type can be registered for it."""

    def __init__(self, path: str, candidates: tuple[type[BaseModel], ...]) -> None:
        """Refuse by naming the field and what it could have been.

        Args:
            path: The dotted path of the field on the settings root.
            candidates: The model types the annotation allows.
        """
        self.path = path
        names = ", ".join(candidate.__name__ for candidate in candidates)
        msg = f"The {path} settings field names more than one model ({names}), so no one type can be registered for it."
        super().__init__(msg)


class SectionOptionalError(Exception):
    """A settings field allows None, and a settings section cannot be optional."""

    def __init__(self, path: str) -> None:
        """Refuse by naming the field.

        Args:
            path: The dotted path of the field on the settings root.
        """
        self.path = path
        msg = f"The {path} settings field allows None, and a settings section cannot be optional."
        super().__init__(msg)
