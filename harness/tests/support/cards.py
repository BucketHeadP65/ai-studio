"""Workflows that select one knowledge base through their card, for tests of what a schedule or a run selects."""

from __future__ import annotations

from core.ai.workflows.contracts import (
    KNOWLEDGE_BASE_CONTROL,
    FromNode,
    FromParameter,
    InputPresentation,
    ParameterSpec,
    RunNode,
    SavedWorkflow,
    Workflow,
    WorkflowPresentation,
)


def base_workflow(name: str) -> Workflow:
    """A one-step workflow over the knowledge base its `base` parameter names."""
    return Workflow(
        name=name,
        version=1,
        description="one step over one knowledge base",
        parameters={"base": ParameterSpec(description="the knowledge base", json_schema={"type": "string", "minLength": 1})},
        nodes=(RunNode(id="read", work="read_base", inputs={"base": FromParameter(parameter="base")}),),
        output=FromNode(node="read"),
    )


def base_card(workflow_id: str) -> SavedWorkflow:
    """The card of a `base_workflow`, declaring `base` a knowledge-base selector."""
    return SavedWorkflow(
        id=workflow_id,
        title=workflow_id,
        summary="",
        presentation=WorkflowPresentation(inputs=(InputPresentation(key="base", label="Knowledge base", control=KNOWLEDGE_BASE_CONTROL),)),
    )
