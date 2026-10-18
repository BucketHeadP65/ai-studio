"""Promotion: an active custom capability joins the capability registry, so a custom agent can declare it by name.

A capability qualifies when it declares a serialization name (`get_serialization_name()` not
overridden to return `None`) and is declared as a dataclass on the class itself, the shape every
built-in capability has and the one pydantic-ai's own registry validates. A frozen dataclass is not
required, because no `AbstractCapability` subclass can be one.

Promotion is the owner's act only, through `harness promote|demote capability <name>`, with no tool
the agent can call. It requires the capability active and refuses a pending one, pointing at
`activate` first. The promoted names live in `promoted.json` beside the capability manifest, sorted
and deduplicated. Callers read `promoted_capability_types` and `capability_grants_for` once per
environment build or session, never per request.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING, Literal

from core.files import atomic_write
from pydantic import BaseModel, ValidationError
from pydantic_ai_harness.capability_creation import CapabilityValidationError, load_capability_instance

from harness.features.selfauthoring.domain import CapabilityPromoted
from harness.features.selfauthoring.exceptions import PromotionError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai_harness.capability_creation import AuthoredCapability

    from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities


def _is_declared_dataclass(cls: type) -> bool:
    """Whether `cls` itself, not merely a base, was decorated `@dataclass`."""
    return "__dataclass_fields__" in cls.__dict__


def _contract_problems(cls: type[AbstractCapability[object]]) -> list[str]:
    """Which half(s) of the `CapabilitySpec` contract `cls` is missing. Empty when it meets both."""
    problems: list[str] = []
    if cls.get_serialization_name() is None:
        problems.append(
            f"{cls.__name__} does not declare a serialization name (`get_serialization_name()` "
            f"returns None). Override it as a classmethod returning a stable name"
        )
    if not _is_declared_dataclass(cls):
        problems.append(f"{cls.__name__} is not declared as a dataclass on the class itself. Decorate it `@dataclass`")
    return problems


class _PromotedManifest(BaseModel):
    """The persisted promoted list: one name per promoted capability, sorted for a clean diff."""

    names: list[str] = []


def _promoted_path(capabilities_directory: Path) -> Path:
    return capabilities_directory / "promoted.json"


def _load_promoted_names(capabilities_directory: Path) -> list[str]:
    path = _promoted_path(capabilities_directory)
    if not path.exists():
        return []
    try:
        return _PromotedManifest.model_validate_json(path.read_text(encoding="utf-8")).names
    except (OSError, ValidationError, ValueError):
        return []


def _save_promoted_names(capabilities_directory: Path, names: Sequence[str]) -> None:
    capabilities_directory.mkdir(parents=True, exist_ok=True)
    manifest = _PromotedManifest(names=sorted(set(names)))
    atomic_write(_promoted_path(capabilities_directory), manifest.model_dump_json(indent=2))


def _record(capabilities: FileCustomCapabilities, name: str) -> AuthoredCapability | None:
    for record in capabilities.list_all():
        if record.name == name:
            return record
    return None


def promote_capability(capabilities: FileCustomCapabilities, name: str) -> AuthoredCapability:
    """Promote the active custom capability `name` into the capability registry.

    The class is loaded through the same loader activation runs it with, then checked against the
    promotion contract. A refusal names exactly which half is missing. Promoting an already promoted
    name succeeds again without a second entry. Every call records the act, whether or not the
    promoted list changed.

    Args:
        capabilities: The custom capabilities `name` is one of.
        name: The capability's name.

    Returns:
        The manifest entry, so the caller can report which class was promoted.

    Raises:
        PromotionError: `name` is not a custom capability, is not active (the message names
            `harness activate capability <name>`), could not be loaded, or its class does not meet
            the promotion contract.
    """
    record = _record(capabilities, name)
    if record is None:
        msg = f"No custom capability named {name!r}."
        raise PromotionError(msg)
    if record.status != "active":
        msg = f"capability {name!r} is not active. Run `harness activate capability {name}` first"
        raise PromotionError(msg)
    try:
        instance = load_capability_instance(capabilities.directory / record.module_file)
    except CapabilityValidationError as exc:
        msg = f"capability {name!r} could not be loaded for promotion: {exc}"
        raise PromotionError(msg) from exc
    problems = _contract_problems(type(instance))
    if problems:
        msg = f"capability {name!r} does not meet the promotion contract: {'; '.join(problems)}"
        raise PromotionError(msg)
    names = _load_promoted_names(capabilities.directory)
    if name not in names:
        names.append(name)
        _save_promoted_names(capabilities.directory, names)
    capabilities.activations.record(CapabilityPromoted(name=name, action="activate", via="cli"))
    return record


def demote_capability(capabilities: FileCustomCapabilities, name: str, *, via: Literal["cli", "agent"] = "cli") -> bool:
    """Remove `name` from the promoted list and record the act from `via`.

    This is the one place a name ever leaves the promoted list. The owner's `harness demote capability
    <name>` calls it, and so does `FileCustomCapabilities` whenever a promoted capability is
    deactivated, disabled or overwritten, each with the surface that act came through. A name that was
    never promoted is a no-op that records nothing, so every deactivation may call this unconditionally.

    Args:
        capabilities: The custom capabilities `name` is one of.
        name: The capability's name.
        via: The surface the act came through.

    Returns:
        Whether `name` was promoted.
    """
    names = _load_promoted_names(capabilities.directory)
    if name not in names:
        return False
    names.remove(name)
    _save_promoted_names(capabilities.directory, names)
    capabilities.activations.record(CapabilityPromoted(name=name, action="deactivate", via=via))
    return True


def promoted_names(capabilities_directory: Path) -> frozenset[str]:
    """Every promoted capability's name, read fail-soft: it never loads a class and never raises.

    A listing renders from this even while a promoted entry's source cannot be resolved.

    Args:
        capabilities_directory: The folder holding the custom capabilities and the promoted list.

    Returns:
        The promoted names. Empty when the list is missing or unreadable.
    """
    return frozenset(_load_promoted_names(capabilities_directory))


def promoted_capability_types(capabilities: FileCustomCapabilities) -> tuple[type[AbstractCapability[object]], ...]:
    """Every promoted capability's class, loaded fresh through the capability loader.

    Every site that takes custom capability types reads this one tuple, among them `AgentCreation`,
    `Agent.from_spec` and `validate_agent_document`. A promoted name whose capability is not active is
    refused before its code is loaded, so a capability the owner has not reviewed in its current form
    never runs here.

    Args:
        capabilities: The custom capabilities the promoted names belong to.

    Returns:
        The classes, in the promoted list's order.

    Raises:
        PromotionError: A promoted name names no custom capability or one that is not active, both
            naming the promoted list's file, or its class could not be loaded.
    """
    types: list[type[AbstractCapability[object]]] = []
    records = {record.name: record for record in capabilities.list_all()}
    for name in _load_promoted_names(capabilities.directory):
        record = records.get(name)
        if record is None:
            msg = (
                f"promoted capability {name!r} names no custom capability. Its source "
                f"may have been removed. Check {_promoted_path(capabilities.directory)}"
            )
            raise PromotionError(msg)
        if record.status != "active":
            msg = (
                f"promoted capability {name!r} is not active. Promotion is revoked automatically on "
                f"deactivation and re-authoring, so this means {_promoted_path(capabilities.directory)} disagrees "
                f"with the manifest. Demote it or reactivate and re-promote it"
            )
            raise PromotionError(msg)
        try:
            instance = load_capability_instance(capabilities.directory / record.module_file)
        except CapabilityValidationError as exc:
            msg = f"promoted capability {name!r} could not be loaded: {exc}"
            raise PromotionError(msg) from exc
        types.append(type(instance))
    return tuple(types)


def promoted_capabilities_digest(capabilities: FileCustomCapabilities) -> str:
    """A stable digest over every promoted capability's name and its current source bytes.

    The environment stamps carry it, so a promoted capability whose source changed under an unchanged
    name shows as drift. It reads the promoted list, the manifest and the source files fresh on every
    call. A promoted name that cannot be resolved now (source gone, not active) is left out rather than
    raised, which still changes the digest against a run where it resolved.

    Args:
        capabilities: The custom capabilities the promoted names belong to.

    Returns:
        The digest, as SHA-256 hex.
    """
    records = {record.name: record for record in capabilities.list_all()}
    pairs: list[tuple[str, str]] = []
    for name in _load_promoted_names(capabilities.directory):
        record = records.get(name)
        if record is None or record.status != "active":
            continue
        try:
            source = (capabilities.directory / record.module_file).read_bytes()
        except OSError:
            continue
        pairs.append((name, hashlib.sha256(source).hexdigest()))
    canonical = json.dumps(sorted(pairs), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def capability_grants_for(types: Sequence[type[AbstractCapability[object]]]) -> tuple[str, ...]:
    """The capability grants a worker document may declare: exactly the serialization names of `types`.

    Args:
        types: The promoted capability classes.

    Returns:
        Their serialization names, in order. Every promoted class has one, so none is dropped.
    """
    names: list[str] = []
    for cls in types:
        name = cls.get_serialization_name()
        if name is not None:
            names.append(name)
    return tuple(names)
