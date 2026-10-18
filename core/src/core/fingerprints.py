"""SHA-256 fingerprints of text and of pydantic models, and the canonical text of a set of names."""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


def content_fingerprint(content: str) -> str:
    """The SHA-256 of exact UTF-8 content.

    Args:
        content: The text to hash.

    Returns:
        The hex digest.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def model_fingerprint(model: BaseModel) -> str:
    """The hash of a model's canonical serialized content, versions and provenance included.

    Args:
        model: The model to hash.

    Returns:
        The hex digest.
    """
    return content_fingerprint(json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def content_of(values: tuple[str, ...]) -> str:
    """One canonical string over a set of names, so the same set always hashes the same way.

    Args:
        values: The names, in any order.

    Returns:
        The sorted names as compact JSON.
    """
    return json.dumps(sorted(values), separators=(",", ":"), ensure_ascii=False)
