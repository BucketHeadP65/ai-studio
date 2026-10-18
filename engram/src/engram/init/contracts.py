"""The public surface of the knowledge base start-up slice.

Each name resolves on first access, so asking for `scaffold`, `write_scaffold` or a template name
loads no alembic. `resolve_db_url` answers a credentialed DSN, so pass it on and
never log or echo it.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from engram.init.domain.models import InitReport
    from engram.init.domain.scaffold import scaffold
    from engram.init.exceptions import SchemaAheadOfCodeError, SchemaBehindHeadError, SchemaRevisionError
    from engram.init.infrastructure.db import (
        assert_schema_at_head,
        code_head_revision,
        load_vocabulary_sync,
        resolve_db_url,
        upgrade_schema,
    )
    from engram.init.infrastructure.templates import Template, list_templates, load_template
    from engram.init.infrastructure.writer import write_scaffold

__all__ = [
    "InitReport",
    "SchemaAheadOfCodeError",
    "SchemaBehindHeadError",
    "SchemaRevisionError",
    "Template",
    "assert_schema_at_head",
    "code_head_revision",
    "list_templates",
    "load_template",
    "load_vocabulary_sync",
    "resolve_db_url",
    "scaffold",
    "upgrade_schema",
    "write_scaffold",
]

_LAZY_IMPORTS = {
    "InitReport": "engram.init.domain.models",
    "scaffold": "engram.init.domain.scaffold",
    "write_scaffold": "engram.init.infrastructure.writer",
    "Template": "engram.init.infrastructure.templates",
    "list_templates": "engram.init.infrastructure.templates",
    "load_template": "engram.init.infrastructure.templates",
    "SchemaRevisionError": "engram.init.exceptions",
    "SchemaBehindHeadError": "engram.init.exceptions",
    "SchemaAheadOfCodeError": "engram.init.exceptions",
    "assert_schema_at_head": "engram.init.infrastructure.db",
    "code_head_revision": "engram.init.infrastructure.db",
    "resolve_db_url": "engram.init.infrastructure.db",
    "upgrade_schema": "engram.init.infrastructure.db",
    "load_vocabulary_sync": "engram.init.infrastructure.db",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
