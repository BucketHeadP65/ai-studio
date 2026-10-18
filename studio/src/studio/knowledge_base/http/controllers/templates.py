"""The template catalog the create dialog offers."""

from engram.init.contracts import list_templates
from fastapi import APIRouter
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import TemplateOut, TemplatesResult


class TemplatesController:
    """Every `templates/<slug>` a new knowledge base can be started from."""

    router = APIRouter(prefix="/api/templates", tags=["templates"], route_class=WireupRoute)

    @router.get("", summary="List the knowledge base templates", operation_id="listTemplates")
    async def list_templates(self) -> TemplatesResult:
        """Every template on disk, each with its slug, title and folder."""
        return TemplatesResult(templates=tuple(TemplateOut(slug=found.slug, title=found.title, path=found.path) for found in list_templates()))
