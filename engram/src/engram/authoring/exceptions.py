"""The authoring slice's errors."""


class NoteRefusedError(Exception):
    """The payload does not satisfy the knowledge base's grammar. The message lists the findings."""
