"""What the browser says about where the person stands, and how a turn carries it to the model.

The block rides the turn's **user prompt** behind a sentinel fence, never the system block.
Everything that renders or derives the person's own words back out of a prompt (the conversation
panel, transcript bubbles, the projection's title excerpt) sheds the fence with `strip_page_context`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PAGE_CONTEXT_OPEN = "<page-context>"
PAGE_CONTEXT_CLOSE = "</page-context>"
RENDERED_CAP = 4_000
"""Largest fence (both sentinels included) a turn may carry: extras are shed first, then the trail."""

_MASTER_ROLE_LINE = (
    "You are the studio's follow-me companion. The person's current page rides each message inside "
    "this fence. Pull anything deeper through your own read tools."
)
_TRAIL_FLOOR = 3
"""Newest trail steps kept when the fence is over the cap with extras already shed."""


def _defang(value: str) -> str:
    """Either fence sentinel found inside `value`, rewritten to its square-bracket form."""
    return value.replace(PAGE_CONTEXT_CLOSE, "[/page-context]").replace(PAGE_CONTEXT_OPEN, "[page-context]")


class TrailStep(BaseModel):
    """One page visited since the previous turn."""

    model_config = ConfigDict(extra="forbid")

    url: str
    at: datetime

    @field_validator("url", mode="after")
    @classmethod
    def _defang_url(cls, value: str) -> str:
        """Either fence sentinel in `url`, rewritten so a trail entry cannot fake a boundary."""
        return _defang(value)


PageTargetType = Literal["tab", "input", "region"]
"""What a registered target is: a tab group `set_tab` switches, an input `prefill` fills, or a region `point` highlights."""

TARGETS_SHOWN = 10
"""Targets listed by name in the fence. The rest are counted, so a busy page cannot crowd the person's words out."""


class TargetDecl(BaseModel):
    """One thing the current page lets the companion touch, declared the way a tool is declared."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: PageTargetType
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    """What the target accepts, as prose per key (a tab group lists its tabs under `tabs`)."""

    @field_validator("name", "description", mode="after")
    @classmethod
    def _defang_scalar(cls, value: str) -> str:
        """Either fence sentinel, rewritten so a target cannot fake a boundary."""
        return _defang(value)

    @field_validator("params", mode="after")
    @classmethod
    def _defang_params(cls, value: dict[str, str]) -> dict[str, str]:
        """Either fence sentinel in a key or value, rewritten so none can fake a boundary."""
        return {_defang(key): _defang(item) for key, item in value.items()}


class PageContext(BaseModel):
    """Where the person is in AI Factory Studio, as the browser posts it beside a chat message.

    Sentinel substrings inside field values are rewritten to their square-bracket forms so the
    fence's boundaries stay unambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    """Path plus query, no origin."""
    page: str
    """The station's name for itself, in the SPA's own words. Any string is accepted, and the server only renders it as prose."""
    ids: dict[str, str] = Field(default_factory=dict)
    """Named ids in view (run, session, workflow, knowledge base, project, schedule)."""
    tab: str = ""
    trail: list[TrailStep] = Field(default_factory=list, max_length=10)
    """Pages visited since the previous turn, oldest first. At most 10."""
    extras: dict[str, str] = Field(default_factory=dict)
    """State only the client holds (an unsaved draft), from pages that registered a callback."""
    targets: list[TargetDecl] = Field(default_factory=list, max_length=30)
    """What the page registered for the verbs to act on. At most 30."""

    @field_validator("url", "page", "tab", mode="after")
    @classmethod
    def _defang_scalar(cls, value: str) -> str:
        """Either fence sentinel in a scalar field, rewritten so it cannot fake a boundary."""
        return _defang(value)

    @field_validator("ids", "extras", mode="after")
    @classmethod
    def _defang_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        """Either fence sentinel in a mapping's keys and values, rewritten so none can fake a boundary."""
        return {_defang(key): _defang(item) for key, item in value.items()}


def compose_prompt(message: str, context: PageContext | None, *, session_kind: str = "chat") -> str:
    """The prompt a turn actually sends: the fenced context (when given), then the person's text.

    The fence is at most `RENDERED_CAP` characters. Over it, the page extras are left out first,
    then all but the newest three trail steps, and last the fence is cut short.

    Args:
        message: The person's text.
        context: Where the person stands, or `None` to send the text alone.
        session_kind: What the session is for. A `"master"` session's fence opens with the
            companion's role line.

    Returns:
        The prompt to send.
    """
    if context is None:
        return message
    return f"{_fence(context, session_kind=session_kind)}\n\n{message}"


def strip_page_context(text: str) -> str:
    """The person's own words, with a leading fence removed. Text without one is returned as it is.

    A message that itself opens with the sentinel is read as a fence, and trims the same way in
    every display derivation.

    Args:
        text: A prompt as a turn sent it.

    Returns:
        The text after the fence, or `text` unchanged when it does not open with a whole fence.
    """
    if not text.startswith(PAGE_CONTEXT_OPEN):
        return text
    close = text.find(PAGE_CONTEXT_CLOSE)
    if close == -1:
        return text
    return text[close + len(PAGE_CONTEXT_CLOSE) :].lstrip()


def _fence(context: PageContext, *, session_kind: str) -> str:
    fence = _render(context, session_kind=session_kind, extras=True, trail=context.trail)
    if len(fence) <= RENDERED_CAP:
        return fence
    fence = _render(context, session_kind=session_kind, extras=False, trail=context.trail)
    if len(fence) <= RENDERED_CAP:
        return fence
    fence = _render(context, session_kind=session_kind, extras=False, trail=context.trail[-_TRAIL_FLOOR:])
    return fence if len(fence) <= RENDERED_CAP else fence[: RENDERED_CAP - len(PAGE_CONTEXT_CLOSE)] + PAGE_CONTEXT_CLOSE


def _render(context: PageContext, *, session_kind: str, extras: bool, trail: list[TrailStep]) -> str:
    lines = [PAGE_CONTEXT_OPEN]
    if session_kind == "master":
        lines.append(_MASTER_ROLE_LINE)
    lines.append(f"The person is on {context.page} at {context.url}.")
    lines.extend(f"{name}: {value}" for name, value in context.ids.items())
    if context.tab:
        lines.append(f"tab: {context.tab}")
    if context.targets:
        lines.append("Touchable here (point, prefill, set_tab):")
        for decl in context.targets[:TARGETS_SHOWN]:
            params = "".join(f", {key}: {value}" for key, value in decl.params.items())
            lines.append(f"  {decl.name} ({decl.kind}): {decl.description}{params}")
        if len(context.targets) > TARGETS_SHOWN:
            lines.append(f"({len(context.targets) - TARGETS_SHOWN} more targets omitted)")
    if trail:
        lines.append("Since their last message they visited:")
        lines.extend(f"  {step.at.isoformat()}  {step.url}" for step in trail)
    if extras and context.extras:
        lines.append("Page-provided state:")
        lines.extend(f"  {name}: {value}" for name, value in context.extras.items())
    elif context.extras:
        lines.append("(page extras omitted, over the cap)")
    lines.append(PAGE_CONTEXT_CLOSE)
    return "\n".join(lines)
