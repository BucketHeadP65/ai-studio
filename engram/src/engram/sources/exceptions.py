"""The errors a caller of the sources slice handles."""


class ConnectorNotInstalledError(Exception):
    """No installed package registers a declared name under one of the three entry-point groups, or its entry point failed to load."""


class ConversionError(Exception):
    """Raised when the converter cannot produce Markdown for an item."""


class ScanError(Exception):
    """A watch entry could not be scanned. The message is what the report records."""


class SourceQueueUnreadableError(Exception):
    """The source queue file cannot be read. The message says which way it is broken."""
