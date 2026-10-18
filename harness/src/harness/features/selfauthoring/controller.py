"""The self-authoring routes: the owner's review and activation of custom items, the tiers, the plugins and the profile selection.

The acts are the same as the `harness` CLI's, over the same custom items, with the same refusal
wording. The item type is a closed set at the route, so an unknown one fails FastAPI's own path
validation (422) before any custom item is touched. An unknown name within an item type is a 404. A
name that exists but cannot be acted on yet (broken code, not yet active) is a 400 naming the stored
reason.

Every literal path is declared before `GET /{kind}`, because the wildcard would otherwise swallow
`tiers`, `models`, `selection` and `plugins`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Tier
from fastapi import APIRouter, HTTPException
from pydantic_ai_harness.capability_creation import AuthoredCapability
from wireup import Injected
from wireup.integration.fastapi import WireupRoute

from harness import plugins
from harness.adapters.contracts import TIER_ROLES, ModelList, ModelTiers, TierSetting, clear_tier, set_tier, tier_in_effect, tiers_in_effect
from harness.domain.contracts import PluginSet, ProfileSelected, SessionEvents, TierSelected
from harness.features.selfauthoring.api_models import (
    HarnessAction,
    HarnessArtifact,
    HarnessComposition,
    HarnessDeletion,
    HarnessListing,
    ModelsListing,
    PluginsListing,
    PluginState,
    PromotionAction,
    Selection,
    TierBody,
    TiersListing,
    TierView,
)
from harness.features.selfauthoring.domain import CUSTOM_ITEM_DIRECTORIES, CustomItemType
from harness.features.selfauthoring.exceptions import PromotionError, SelectionError
from harness.features.selfauthoring.infrastructure.activation_log import NoActivations, activations_under
from harness.features.selfauthoring.infrastructure.promotion import demote_capability, promote_capability, promoted_names
from harness.features.selfauthoring.internal_contracts import ActivationLog
from harness.features.selfauthoring.service import custom_items_for, descriptor_of, select_profile, selected_profile_name
from harness.features.sessions.contracts import Sessions
from harness.settings import RunSettings, SelfAuthoringSettings, StateSettings, custom_root

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.selfauthoring.domain import CustomAgent, CustomProfile, CustomStep


def _refused(error: Exception) -> HTTPException:
    """The 400 an act answers when the slice refuses it, carrying the refusal's own message."""
    return HTTPException(status_code=400, detail=str(error))


async def tiers_listing(tiers: ModelTiers) -> TiersListing:
    """The three tiers in effect, each rendered against the list this deployment can reach.

    Args:
        tiers: The owner's tier settings and the model list they are checked against.

    Returns:
        The tiers, in listing order.
    """
    models = tiers.models
    await models.warm()
    views = []
    for tier, effect in tiers_in_effect(tiers).items():
        entry = models.entry(effect.setting.model)
        if entry is None:  # pragma: no cover, tier_in_effect only answers listed ids
            continue
        views.append(TierView(tier=tier, role=TIER_ROLES[tier], model=entry, thinking=effect.setting.thinking, default=effect.default))
    return TiersListing(tiers=tuple(views))


def custom_item_row(record: AuthoredCapability | CustomStep | CustomAgent | CustomProfile, promoted: frozenset[str]) -> HarnessArtifact:
    """One manifest entry as a listing row, whose `next_step` names the one forward act it is ready for.

    Args:
        record: The custom item's manifest entry.
        promoted: The promoted capability names.

    Returns:
        The row.
    """
    is_capability = isinstance(record, AuthoredCapability)
    is_promoted = record.name in promoted if is_capability else None
    next_step = None
    if record.status == "disabled":
        if record.last_error is None:
            next_step = "activate"
    elif is_capability and not is_promoted:
        next_step = "promote"
    return HarnessArtifact(
        name=record.name,
        status=record.status,
        descriptor=descriptor_of(record),
        last_error=record.last_error,
        promoted=is_promoted,
        next_step=next_step,
    )


@dataclass(frozen=True)
class SelfAuthoringDeployment:
    """What this deployment is, as the owner's own surface reads it and a console act forwards it.

    Four sections that always travel together here: a route reads one of them and a console act
    standing in for that route has to hand it the same four.
    """

    state: StateSettings
    """Where the plugin switches and the profile selection this deployment reads live."""
    self_authoring: SelfAuthoringSettings
    """Where owner-reviewed custom items live."""
    run: RunSettings
    """Which engine runs a workflow."""
    tiers: ModelTiers
    """How a tier name becomes a model here."""


class SelfAuthoringController:
    """The self-authoring routes under `/api/harness`: custom items, tiers, plugins and the profile selection."""

    router = APIRouter(prefix="/api/harness", tags=["selfauthoring"], route_class=WireupRoute)

    def __init__(self, settings: SelfAuthoringDeployment, session_log: SessionEvents, sessions: Sessions) -> None:
        """Hold what this deployment is, where an act is recorded, and the sessions it holds open.

        Args:
            settings: The deployment facts every route here reads.
            session_log: Where a tier, profile or plugin change is recorded.
            sessions: Every session this process holds open. Its default is the one the composition
                answer describes and the one a profile switch refuses to interrupt.
        """
        self._settings = settings
        self._session_log = session_log
        self._sessions = sessions

    def _activations(self) -> ActivationLog:
        """Where an act this controller performs is recorded, under this deployment's home."""
        return activations_under(self._settings.state)

    @router.get("/composition")
    async def composition(self) -> HarnessComposition:
        """The live composition: model, engine, registered steps, promoted capabilities, active counts."""
        root = self._root()
        item_types: tuple[CustomItemType, ...] = ("capability", "step", "agent", "profile")
        return HarnessComposition(
            model=tier_in_effect(self._settings.tiers, "orchestrator").setting.model,
            run_engine=self._settings.run.run_engine,
            steps_registered=self._sessions.default.registry.names,
            promoted=tuple(sorted(promoted_names(root / CUSTOM_ITEM_DIRECTORIES["capability"]))),
            active={
                item_type: sum(
                    1 for record in custom_items_for(item_type, root, activations=NoActivations()).list_all() if record.status == "active"
                )
                for item_type in item_types
            },
        )

    @router.get("/tiers")
    async def get_tiers(self) -> TiersListing:
        """The three tiers in effect now."""
        return await tiers_listing(self._settings.tiers)

    @router.put("/tiers/{tier}")
    async def put_tier(self, tier: Tier, body: TierBody) -> TiersListing:
        """Put `tier` on `body.model` at `body.thinking`. In effect on the very next request, no restart."""
        try:
            set_tier(self._settings.tiers, tier, TierSetting(model=body.model, thinking=body.thinking))
        except ValueError as exc:
            raise _refused(exc) from exc
        self._session_log.record(TierSelected(tier=tier, model=body.model, thinking=body.thinking))
        return await tiers_listing(self._settings.tiers)

    @router.delete("/tiers/{tier}")
    async def delete_tier(self, tier: Tier) -> TiersListing:
        """Put `tier` back on its default."""
        clear_tier(self._settings.tiers, tier)
        self._session_log.record(TierSelected(tier=tier, model=None, thinking=None))
        return await tiers_listing(self._settings.tiers)

    @router.get("/models")
    async def get_models(self, models: Injected[ModelList], q: str = "") -> ModelsListing:
        """Every listed model whose id or name contains `q`, pinned first, at most fifty."""
        return ModelsListing(models=await models.search(q))

    @router.get("/selection")
    async def get_selection(self) -> Selection:
        """Which profile is in effect right now. Registered before the `{kind}` routes, like plugins."""
        return Selection(name=selected_profile_name(self._settings.state))

    @router.put("/selection")
    async def put_selection(self, body: Selection) -> Selection:
        """Put one active profile in effect, or clear with `null`. Validates the whole profile first.

        Refused mid-conversation: the running dialogue was produced under the current composition,
        so the person forgets it (or starts fresh) before the ground changes under it.
        """
        if body.name is not None and len(self._sessions.default.conversation.history) > 0:
            raise HTTPException(status_code=409, detail="A conversation is in progress. Forget it first, then switch the profile.")
        try:
            select_profile(body.name, root=self._root(), state=self._settings.state)
            self._session_log.record(ProfileSelected(name=body.name))
        except SelectionError as exc:
            raise _refused(exc) from exc
        return Selection(name=selected_profile_name(self._settings.state))

    @router.get("/plugins")
    async def list_plugins(self) -> PluginsListing:
        """Every built-in plugin with its live state: the toggleable ones first, then the always-on ones.

        Registered before the `{kind}` routes so the literal path wins the match.
        """
        closed = plugins.disabled_plugins(self._settings.state)
        rows = [
            PluginState(name=plugin.name, description=plugin.description, enabled=plugin.name not in closed, fixed=False)
            for plugin in plugins.TOGGLEABLE
        ]
        rows += [PluginState(name=plugin.name, description=plugin.description, enabled=True, fixed=True) for plugin in plugins.ALWAYS_ON]
        return PluginsListing(plugins=rows)

    @router.post("/plugins/{name}/enable")
    async def enable_plugin(self, name: str) -> PluginState:
        """Open one plugin. Takes effect on the very next chat turn, no restart."""
        return self.flip_plugin(name, enabled=True)

    @router.post("/plugins/{name}/disable")
    async def disable_plugin(self, name: str) -> PluginState:
        """Close one plugin. Takes effect on the very next chat turn, no restart."""
        return self.flip_plugin(name, enabled=False)

    def flip_plugin(self, name: str, *, enabled: bool) -> PluginState:
        """Turn one plugin on or off, refused per the catalog.

        An always-on plugin is a 400, because it exists and cannot move, and an unknown one is a 404. A
        selected profile also refuses the flip with a 400 that names it, because the plugins follow the
        profile. The follow-me console acts run this same body in process.

        Args:
            name: The plugin's name.
            enabled: Whether to turn it on.

        Returns:
            The plugin's state after the flip.

        Raises:
            HTTPException: The flip is refused, with the status above.
        """
        if any(plugin.name == name for plugin in plugins.ALWAYS_ON):
            raise HTTPException(status_code=400, detail=f"{name!r} is always on and cannot be toggled.")
        described = next((plugin for plugin in plugins.TOGGLEABLE if plugin.name == name), None)
        if described is None:
            raise HTTPException(status_code=404, detail=f"No plugin named {name!r}.")
        try:
            plugins.set_plugin(self._settings.state, name, enabled=enabled)
        except ValueError as exc:
            raise _refused(exc) from exc
        self._session_log.record(PluginSet(plugin=name, enabled=enabled))
        return PluginState(
            name=name, description=described.description, enabled=plugins.plugin_enabled(self._settings.state, name), fixed=False
        )

    @router.get("/{kind}")
    async def list_harness(self, kind: CustomItemType) -> HarnessListing:
        """Every custom item of `kind`, in the shape `harness list` prints, as JSON."""
        root = self._root()
        items = custom_items_for(kind, root, activations=self._activations())
        promoted = promoted_names(root / CUSTOM_ITEM_DIRECTORIES["capability"]) if kind == "capability" else frozenset()
        return HarnessListing(kind=kind, artifacts=[custom_item_row(record, promoted) for record in items.list_all()])

    @router.post("/{kind}/{name}/activate")
    async def activate(self, kind: CustomItemType, name: str) -> HarnessAction:
        """Flip `name` active: it loads on the next agent run, and the act is recorded.

        Refused, naming the stored error, when the last write of `name` failed validation. Activating
        known-broken code is never the fix.
        """
        items = custom_items_for(kind, self._root(), activations=self._activations())
        try:
            found = items.activate(name)
        except ValueError as exc:
            raise _refused(exc) from exc
        if not found:
            raise HTTPException(status_code=404, detail=f"No custom {kind} named {name!r}.")
        return HarnessAction(kind=kind, name=name, status="active")

    @router.post("/{kind}/{name}/disable")
    async def disable(self, kind: CustomItemType, name: str) -> HarnessAction:
        """Flip `name` disabled: it stops loading on the next agent run, and the act is recorded."""
        items = custom_items_for(kind, self._root(), activations=self._activations())
        if not items.deactivate(name):
            raise HTTPException(status_code=404, detail=f"No custom {kind} named {name!r}.")
        return HarnessAction(kind=kind, name=name, status="disabled")

    @router.delete("/{kind}/{name}")
    async def delete_artifact(self, kind: CustomItemType, name: str) -> HarnessDeletion:
        """Remove `name` outright: its manifest entry and its source file, the act recorded.

        Refused while `name` is active, so the composition never changes as a side effect of tidying
        the library. Disable it first.
        """
        items = custom_items_for(kind, self._root(), activations=self._activations())
        try:
            found = items.delete(name)
        except ValueError as exc:
            raise _refused(exc) from exc
        if not found:
            raise HTTPException(status_code=404, detail=f"No custom {kind} named {name!r}.")
        return HarnessDeletion(kind=kind, name=name)

    @router.post("/capability/{name}/promote")
    async def promote(self, name: str) -> PromotionAction:
        """Join `name` to the capability registry. Capabilities only, and `name` must already be active."""
        capabilities = custom_items_for("capability", self._root(), activations=self._activations())
        if not any(record.name == name for record in capabilities.list_all()):
            raise HTTPException(status_code=404, detail=f"No custom capability named {name!r}.")
        try:
            promote_capability(capabilities, name)
        except PromotionError as exc:
            raise _refused(exc) from exc
        return PromotionAction(name=name, promoted=True)

    @router.post("/capability/{name}/demote")
    async def demote(self, name: str) -> PromotionAction:
        """Remove `name` from the capability registry. Capabilities only."""
        capabilities = custom_items_for("capability", self._root(), activations=self._activations())
        if not demote_capability(capabilities, name):
            raise HTTPException(status_code=404, detail=f"Capability {name!r} is not currently promoted.")
        return PromotionAction(name=name, promoted=False)

    def _root(self) -> Path:
        """The custom root, resolved fresh on every call, so a `harness` invocation and a route agree on what is on disk."""
        return custom_root(self._settings.self_authoring)
