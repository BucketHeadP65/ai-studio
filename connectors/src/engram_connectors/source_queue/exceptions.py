"""The errors the source queue connector and its resolvers raise."""


class ResolveError(Exception):
    """A row could not be resolved. The message is what the queue records as the reason."""
