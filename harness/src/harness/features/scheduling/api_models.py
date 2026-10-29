"""The request and response bodies of the schedule routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.ai.workflows.contracts import WorkflowArguments
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from harness.domain.contracts import GateMode


class ScheduleIn(BaseModel):
    """A new standing intent: what to run, with what, and how often."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    workflow_id: str
    arguments: WorkflowArguments = Field(default_factory=dict)
    """One JSON value per declared parameter, admitted against that parameter's own schema before
    the row is written. Text under a parameter that cannot hold it is read as JSON once, so a
    client with nothing but strings still writes a schedule that fires."""
    cadence: str
    """`every <N><s, m, h, or d>`, a 5 field cron expression, `on refresh`, `after <workflow name>`, or `on index <knowledge base>`
    with an optional `quiet <N><s, m, or h>` of at most 24 hours."""
    gate_policy: str = "wait"
    """`wait` to hold a gate for a person forever, or `fail_after:<seconds>`."""
    gate_mode: GateMode = "wait"
    """`wait`, `orchestrate`, or `auto`."""
    capacity_key: str | None = None
    """A name shared with other schedules to cap how many of them run at once."""
    capacity_limit: int | None = None
    """How many schedules sharing `capacity_key` may run at once."""
    blackout: str | None = None
    """One or more `HH:MM-HH:MM` daily quiet hours, comma separated."""
    catch_up: bool = False
    """Fire once as soon as a held fire clears, instead of waiting for the next normal turn."""


class SchedulePatch(BaseModel):
    """A partial change to a schedule's settings. An absent field is not changed."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    workflow_id: str | None = None
    arguments: WorkflowArguments | None = None
    cadence: str | None = None
    """`every <N><s, m, h, or d>`, a 5 field cron expression, `on refresh`, `after <workflow name>`, or `on index <knowledge base>`
    with an optional `quiet <N><s, m, or h>` of at most 24 hours."""
    enabled: bool | None = None
    gate_policy: str | None = None
    """`wait` to hold a gate for a person forever, or `fail_after:<seconds>`."""
    gate_mode: GateMode | None = None
    """`wait`, `orchestrate`, or `auto`."""
    capacity_key: str | None = None
    """A name shared with other schedules to cap how many of them run at once."""
    capacity_limit: int | None = None
    """How many schedules sharing `capacity_key` may run at once."""
    blackout: str | None = None
    """One or more `HH:MM-HH:MM` daily quiet hours, comma separated."""
    catch_up: bool | None = None
    """Fire once as soon as a held fire clears, instead of waiting for the next normal turn."""


class CadenceIn(BaseModel):
    """The cadence text the preview route parses without keeping anything."""

    cadence: str
    """`every <N><s, m, h, or d>`, a 5 field cron expression, `on refresh`, `after <workflow name>`, or `on index <knowledge base>`
    with an optional `quiet <N><s, m, or h>` of at most 24 hours."""


class ScheduleOut(BaseModel):
    """One schedule as a listing row: its settings, its tick log, whether it is live and what it spent."""

    id: UUID
    workflow_id: str
    arguments: dict[str, JsonValue]
    cadence: str
    enabled: bool
    gate_policy: str
    gate_mode: GateMode
    capacity_key: str | None
    capacity_limit: int | None
    blackout: str | None
    catch_up: bool
    session_id: str
    created_at: datetime
    last_run_at: datetime | None
    last_outcome: str
    next_run_at: datetime | None
    paused_at: datetime | None
    live: str | None
    spend_today_usd: Decimal
    spend_week_usd: Decimal
    spend_today_unpriced: int = 0
    """Responses in today's figure that nothing could price. Above zero, the figure beside it is a floor."""
    spend_week_unpriced: int = 0
    """The same count over the seven-day figure."""


class RefreshRunIn(BaseModel):
    """One ended refresh run of a knowledge base, as the studio reports it."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="ignore")

    event_id: UUID
    """The studio's id for the refresh run's ending. A second report with the same id arms nothing and tells nobody."""
    knowledge_base: str = Field(min_length=1)
    """The knowledge base's registry name, the name a schedule's knowledge-base argument holds."""
    sources_created: int = Field(default=0, ge=0)
    """How many sources the refresh run created. Zero arms nothing and tells nobody."""
    requested_by_entry: dict[str, int] = Field(default_factory=dict)
    """The new sources each watch-list entry found, by the entry's label, named in the owner's notification."""


class RefreshRunOut(BaseModel):
    """What the harness did about one reported refresh run."""

    repeat: bool = False
    """True when this refresh run was reported before, so nothing was armed and nobody was told."""
    notified: bool = False
    """Whether the owner's notification was delivered."""
    armed: list[UUID] = Field(default_factory=list)
    """The schedules that will fire on the next tick."""


class IndexRunIn(BaseModel):
    """One index of a knowledge base with changed pages, as the studio reports it."""

    model_config = ConfigDict(use_attribute_docstrings=True, extra="ignore")

    event_id: UUID
    """The studio's id for the index. A second report with the same id arms nothing."""
    knowledge_base: str = Field(min_length=1)
    """The knowledge base's registry name, the name an on-index cadence holds."""
    page_ids: list[str] = Field(min_length=1)
    """The changed pages of the index, at least one."""


class IndexRunOut(BaseModel):
    """What the harness did about one reported index."""

    repeat: bool = False
    """True when this index was reported before, so nothing was armed."""
    armed: list[UUID] = Field(default_factory=list)
    """The schedules whose fire moved to the end of their quiet period and that collected the changed pages."""
