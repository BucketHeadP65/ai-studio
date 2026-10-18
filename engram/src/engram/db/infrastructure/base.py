"""The declarative base for the `kb` schema."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class KbBase(DeclarativeBase):
    """Declarative base whose metadata holds every `kb` table.

    A table that maps against it sets `__table_args__ = {"schema": "kb"}`.
    """
