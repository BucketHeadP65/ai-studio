"""The port format maps keeps fetched content behind."""

from typing import Protocol


class ContentCache(Protocol):
    """Where a mapped tool's fetched content waits for `part=` calls.

    Keys are internal (run id, tool name, an argument digest) and never reach the model. A lost entry is never an error,
    only a miss that `FormatMaps` handles by its `refetch_on_miss` setting.
    """

    async def put(self, key: str, text: str) -> None:
        """Store `text` under `key`, replacing any previous entry.

        Args:
            key: The run-scoped key of one call's content.
            text: The content to keep.
        """

    async def get(self, key: str) -> str | None:
        """The text stored under `key`.

        Args:
            key: The run-scoped key of one call's content.

        Returns:
            The stored text, or `None` when nothing is stored under `key` anymore.
        """
