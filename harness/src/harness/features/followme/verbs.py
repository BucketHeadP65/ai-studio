"""The four verbs the browser performs for the companion, declared server-side the way any tool is declared."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ExternalToolset

if TYPE_CHECKING:
    from harness.session import Runtime


def _params(**fields: str) -> dict[str, object]:
    """The JSON schema of an object whose fields are all required strings, each with its description."""
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": text} for name, text in fields.items()},
        "required": list(fields),
        "additionalProperties": False,
    }


VERB_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="navigate",
        description=(
            "Go to a dashboard route. The whole vocabulary, so you never have to guess one: /chat, /workflows (the fleet), "
            "/workflows/draft, /workflows/library, /workflows/schedules, /projects, /knowledge_bases, /harness, /settings, and a "
            "detail page under /workflows/{run_id}, /workflows/library/{workflow_id}, /knowledge_bases/{name} or /projects/{name}. "
            "Anything else is refused. The page context on your next message reflects the new page."
        ),
        parameters_json_schema=_params(url="The route, path and query only."),
    ),
    ToolDefinition(
        name="point",
        description="Scroll to and briefly highlight a registered target on the current page, the look-here verb.",
        parameters_json_schema=_params(target="A target name from the page context."),
    ),
    ToolDefinition(
        name="prefill",
        description="Put text into a registered input without submitting it, so the person can edit and send it.",
        parameters_json_schema=_params(target="An input target name from the page context.", text="The text to put there."),
    ),
    ToolDefinition(
        name="set_tab",
        description="Switch a registered tab group to one of its tabs, named in the target's tabs list.",
        parameters_json_schema=_params(target="A tab target name from the page context.", tab="One of its tabs."),
    ),
)
"""The four verbs as tool definitions, in the order of `VERBS`. The browser performs each one."""


def verb_toolset() -> ExternalToolset[Runtime]:
    """The verbs as an external toolset. A call ends the run deferred, and the browser answers it on the next request.

    Returns:
        The four verbs, under the toolset id `follow-me-verbs`.
    """
    return ExternalToolset(list(VERB_DEFINITIONS), id="follow-me-verbs")
