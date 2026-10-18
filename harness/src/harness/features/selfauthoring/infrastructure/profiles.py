"""The custom profiles on disk: YAML documents naming plugins, custom items and standing instructions, each landing disabled."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import yaml

from core.files import atomic_write
from pydantic import BaseModel, ValidationError

from harness import plugins
from harness.features.selfauthoring.domain import CustomProfile, ProfileActivated, ProfileDoc
from harness.features.selfauthoring.exceptions import ProfileValidationError, SelectionError
from harness.features.selfauthoring.infrastructure.activation_log import record_overwrite
from harness.features.selfauthoring.skills_plugin import registry_skill_names, skill_refusal, unknown_skill_names

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from harness.features.selfauthoring.internal_contracts import ActivationLog

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_profile_document(document_yaml: str, *, known_skill_names: Collection[str] | None = None) -> ProfileDoc:
    """Parse and validate one profile document: a YAML mapping, known fields, real plugin and skill names.

    Args:
        document_yaml: The document, as YAML.
        known_skill_names: The names `skills` is checked against. The registry alone when `None`.

    Returns:
        The parsed document.

    Raises:
        ProfileValidationError: The text is not YAML or not a mapping, it carries an unknown field, it
            names a plugin outside the toggleable catalog (an always-on one included, because a profile
            composes toggleable plugins), or it names a skill absent from `known_skill_names`.
    """
    try:
        raw = yaml.safe_load(document_yaml)
    except yaml.YAMLError as exc:
        msg = f"not valid YAML: {exc}"
        raise ProfileValidationError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"a profile document must be a YAML mapping, got {type(raw).__name__}"
        raise ProfileValidationError(msg)
    try:
        doc = ProfileDoc.model_validate(raw)
    except ValidationError as exc:
        raise ProfileValidationError(str(exc)[:900]) from exc
    for plugin in doc.plugins:
        try:
            plugins.check_toggleable(plugin)
        except ValueError as exc:
            raise ProfileValidationError(str(exc)) from exc
    known_skills = known_skill_names if known_skill_names is not None else registry_skill_names()
    if unknown := unknown_skill_names(doc.skills, known_skills):
        raise ProfileValidationError(skill_refusal(unknown, known_skills))
    return doc


class _Manifest(BaseModel):
    profiles: list[CustomProfile] = []


@dataclass
class FileCustomProfiles:
    """The custom profiles on disk, each landing disabled until the owner activates it.

    Each profile is one `<name>.yaml` under `directory`, indexed by a sibling `manifest.json`, the same
    shape as the other custom items, recording through the same log.
    """

    directory: Path
    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded. Custom profiles that are only read are built
    over `NoActivations`."""
    known_skill_names: Collection[str] | None = None
    """The name space `skills` is checked against. `None` falls back to the registry alone."""

    @property
    def _manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    def _load_manifest(self) -> _Manifest:
        path = self._manifest_path
        if not path.exists():
            return _Manifest()
        try:
            return _Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return _Manifest()

    def _save_manifest(self, manifest: _Manifest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(self._manifest_path, manifest.model_dump_json(indent=2))

    def _upsert(self, record: CustomProfile) -> None:
        manifest = self._load_manifest()
        for index, existing in enumerate(manifest.profiles):
            if existing.name == record.name:
                manifest.profiles[index] = record
                break
        else:
            manifest.profiles.append(record)
        self._save_manifest(manifest)

    def _existing(self, name: str) -> CustomProfile | None:
        for record in self.list_all():
            if record.name == name:
                return record
        return None

    def write(self, name: str, document_yaml: str) -> CustomProfile:
        """Write `document_yaml` to `<name>.yaml`, validate it, and upsert its entry disabled.

        Pending is the only landing state. Overwriting an active profile is itself a deactivation,
        recorded from `agent`. A selected profile's live snapshot is untouched by an overwrite, and
        selecting it again applies the edit.

        Args:
            name: The name to write the document under.
            document_yaml: The complete profile document, as YAML.

        Returns:
            The manifest entry as written, with the validation error when there was one.

        Raises:
            ValueError: `name` is not lowercase letters, digits and underscores starting with a letter.
                Nothing is written.
        """
        if not _NAME_RE.match(name):
            msg = f"invalid profile name {name!r}: use lowercase letters, digits, and underscores, starting with a letter"
            raise ValueError(msg)
        previous = self._existing(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{name}.yaml"
        path.write_text(document_yaml, encoding="utf-8")
        try:
            doc = validate_profile_document(document_yaml, known_skill_names=self.known_skill_names)
            record = CustomProfile(name=name, document_file=path.name, plugins=list(doc.plugins))
        except ProfileValidationError as exc:
            record = CustomProfile(name=name, document_file=path.name, plugins=[], last_error=str(exc))
        self._upsert(record)
        record_overwrite(previous, name, event_type=ProfileActivated, activations=self.activations, revoke=None)
        return record

    def activate(self, name: str) -> bool:
        """Flip `name` active and record the act from `cli`.

        Args:
            name: The profile's name.

        Returns:
            Whether a profile had that name.

        Raises:
            ValueError: The profile's last write failed validation.
        """
        record = self._existing(name)
        if record is not None and record.last_error is not None:
            msg = f"cannot activate profile {name!r}: it failed validation: {record.last_error}"
            raise ValueError(msg)
        return self._set_status(name, "active", action="activate")

    def deactivate(self, name: str) -> bool:
        """Flip `name` disabled and record the act from `cli`.

        Args:
            name: The profile's name.

        Returns:
            Whether a profile had that name.
        """
        return self._set_status(name, "disabled", action="deactivate")

    def _set_status(self, name: str, status: Literal["active", "disabled"], *, action: Literal["activate", "deactivate"]) -> bool:
        manifest = self._load_manifest()
        found = False
        for record in manifest.profiles:
            if record.name == name:
                record.status = status
                found = True
        if found:
            self._save_manifest(manifest)
            self.activations.record(ProfileActivated(name=name, action=action, via="cli"))
        return found

    def delete(self, name: str) -> bool:
        """Remove `name`'s manifest entry and its document, and record the act from `cli`.

        Args:
            name: The profile's name.

        Returns:
            Whether a profile had that name.

        Raises:
            ValueError: `name` is still active. It has to be disabled first.
        """
        record = self._existing(name)
        if record is None:
            return False
        if record.status == "active":
            msg = f"cannot delete profile {name!r} while it is active: disable it first"
            raise ValueError(msg)
        manifest = self._load_manifest()
        manifest.profiles = [entry for entry in manifest.profiles if entry.name != name]
        self._save_manifest(manifest)
        (self.directory / record.document_file).unlink(missing_ok=True)
        self.activations.record(ProfileActivated(name=name, action="delete", via="cli"))
        return True

    def resolve_document(self, name: str) -> ProfileDoc:
        """The parsed, validated document behind one manifest entry.

        Args:
            name: The profile's name.

        Returns:
            The document.

        Raises:
            SelectionError: `name` names no profile, or its document no longer parses.
        """
        record = self._existing(name)
        if record is None:
            msg = f"no custom profile named {name!r}"
            raise SelectionError(msg)
        try:
            return validate_profile_document(
                (self.directory / record.document_file).read_text(encoding="utf-8"), known_skill_names=self.known_skill_names
            )
        except (OSError, ProfileValidationError) as exc:
            msg = f"profile {name!r} cannot be read: {exc}"
            raise SelectionError(msg) from exc

    def list_all(self) -> list[CustomProfile]:
        """Every manifest entry, in insertion order.

        Returns:
            The entries.
        """
        return self._load_manifest().profiles
