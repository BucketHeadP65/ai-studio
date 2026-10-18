"""The login role each private knowledge base is read through, and the password derived for it.

A private knowledge base is visible only to a session that logs in as its own reader role. The role name comes from
the collection alone and the password from the role name and one shared secret, so the indexer that creates the role
and the reader that connects as it agree without storing either.
"""

from __future__ import annotations

import hashlib
import hmac

READER_ROLE = "kb_reader"
"""The read-only role every migration grants the `api` schema to. Each private reader role is a member of it."""

_PRIVATE_READER_PREFIX = f"{READER_ROLE}_"


def private_reader_role(collection: str) -> str:
    """The login role name a private collection is read through: `kb_reader_` and 20 hex digits of the collection's hash.

    Args:
        collection: The knowledge base's collection.

    Returns:
        A role name that is a plain identifier, the same for the same collection everywhere.
    """
    return _PRIVATE_READER_PREFIX + hashlib.sha256(collection.encode()).hexdigest()[:20]


def private_reader_password(role: str, secret: str) -> str:
    """The password of a private reader role, derived from its name and the shared secret.

    Args:
        role: The role name `private_reader_role` gave.
        secret: The deployment's reader secret. It must not be empty.

    Returns:
        64 hex digits.

    Raises:
        ValueError: When `secret` is empty.
    """
    if not secret:
        msg = "a private reader password needs the reader secret, and it is empty"
        raise ValueError(msg)
    return hmac.new(secret.encode(), role.encode(), hashlib.sha256).hexdigest()
