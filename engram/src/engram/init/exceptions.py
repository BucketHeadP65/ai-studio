"""The errors raised when a knowledge base database is not at the code's alembic head."""


class SchemaRevisionError(RuntimeError):
    """The knowledge base database and the code disagree on the alembic revision."""


class SchemaBehindHeadError(SchemaRevisionError):
    """The knowledge base database has not been migrated to the code's alembic head."""


class SchemaAheadOfCodeError(SchemaRevisionError):
    """The knowledge base database carries a revision this code does not know, so the code is the older side."""
