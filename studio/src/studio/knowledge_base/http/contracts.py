"""What code outside the knowledge base HTTP API may use: the application and the registry of knowledge bases.

Each name resolves on first access, so importing this module loads neither the application nor the service.
"""

from typing import TYPE_CHECKING

from core.lazy_imports import lazy_module_attributes

if TYPE_CHECKING:
    from studio.knowledge_base.http.api import create_app
    from studio.knowledge_base.http.service import KnowledgeBaseRegistry

__all__ = ["KnowledgeBaseRegistry", "create_app"]

_LAZY_IMPORTS = {
    "KnowledgeBaseRegistry": "studio.knowledge_base.http.service",
    "create_app": "studio.knowledge_base.http.api",
}


__getattr__, __dir__ = lazy_module_attributes(__name__, _LAZY_IMPORTS, __all__)
