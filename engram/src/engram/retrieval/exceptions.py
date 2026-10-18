"""The errors the read side raises on a refused ref or a refused schema ask."""

from __future__ import annotations

_REGROUND_WITH_SEARCH = "`search`"
"""The default re-grounding phrase every ref-parsing refusal and the leveled description teach.

A caller whose surface does not register `search` passes its own `reground` phrase instead.
"""


class BadRefError(ValueError):
    """Ref did not match `collection:<name>` | `group:<collection>/<app>` | `page:<id>` | `section:<id>`."""


class RefRefusedError(BadRefError):
    """A well-formed ref that is refused outright, whose own message is the answer.

    `leveled_fetch` raises it straight through and never falls back to the `api.fetch` read, as it does
    for a plain `BadRefError`.
    """


class WrongCollectionError(RefRefusedError):
    """`ref` names a real `collection:`/`group:` segment, but not this handler's own knowledge base.

    Raised only when the handler was constructed with a `collection` to enforce. A handler built with
    none accepts any collection. A subclass of `BadRefError` (via `RefRefusedError`) so a multi-knowledge-base caller that
    already retries the next surface on `BadRefError` (`RetrievalKnowledgeBase._open`) keeps doing
    exactly that.
    """

    def __init__(self, served_collection: str, requested_collection: str, *, reground: str = _REGROUND_WITH_SEARCH) -> None:
        """Tell an agent how to re-ground a handle from another knowledge base.

        Args:
            served_collection: The knowledge base this handler actually serves.
            requested_collection: The knowledge base the refused ref named instead.
            reground: How the message tells the agent to get a good ref, `` `search` `` (the
                default) unless the caller's surface does not register it.
        """
        message = (
            f"This server serves the '{served_collection}' knowledge base only. '{requested_collection}' is a different knowledge base. "
            f"Re-ground it with {reground} and fetch a result from this knowledge base."
        )
        super().__init__(message)


class GroupUnknownError(RefRefusedError):
    """`ref` names a group value this collection has never had, such as a typo, not an empty group."""


class DescribeSchemaRefusedError(ValueError):
    """A `describe_schema(objects=...)` ask this handler cannot answer: too many names, or an unknown one.

    It is not a tool error. The tool that wraps the handler translates it into one.
    """


class ReaderRoleOverprivilegedError(RuntimeError):
    """The reader connection logs in as a role that can do more than read the `api` schema.

    The message names the role, every privilege it holds beyond that, and the setting to change.
    """
