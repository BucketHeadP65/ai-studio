"""The custom capabilities on disk, each landing disabled until the owner activates it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic_ai_harness.capability_creation import AuthoredCapability, CapabilityStore

from harness.features.selfauthoring.domain import CapabilityActivated
from harness.features.selfauthoring.infrastructure.activation_log import NoActivations, record_overwrite
from harness.features.selfauthoring.infrastructure.promotion import demote_capability

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.selfauthoring.internal_contracts import ActivationLog


@dataclass
class FileCustomCapabilities(CapabilityStore):
    """The vendor's capability store, with every custom capability landing disabled until the owner activates it.

    Pending is the only landing state. Only `activate` and `deactivate`, driven by the owner, flip an
    entry live or dark, and each act is recorded in the activation log. Deactivating, disabling or
    overwriting a promoted capability also demotes it.
    """

    activations: ActivationLog = field(kw_only=True)
    """Where every act on a custom capability is recorded. Custom capabilities that are only read are built over `NoActivations`."""

    @classmethod
    def read_only(cls, directory: Path) -> FileCustomCapabilities:
        """Custom capabilities over `directory` that record nowhere, for a caller that only reads.

        Args:
            directory: The folder holding the capability modules and the manifest.

        Returns:
            The custom capabilities, built over `NoActivations`.
        """
        return cls(directory, activations=NoActivations())

    def _existing(self, name: str) -> AuthoredCapability | None:
        for record in self.list_all():
            if record.name == name:
                return record
        return None

    def write(self, name: str, code: str) -> AuthoredCapability:
        """Write, validate and upsert `name`, landing its manifest entry disabled.

        A capability that validates cleanly lands disabled exactly like one that failed validation. The
        owner activates it with `harness activate capability <name>`. Overwriting an active capability
        is itself a deactivation: it is recorded from `agent`, the only surface that writes, and the
        capability is demoted when it was promoted.

        Args:
            name: The name to write the capability under.
            code: The complete Python source.

        Returns:
            The manifest entry as written, with the validation error when there was one.

        Raises:
            ValueError: `name` is not lowercase letters, digits and underscores starting with a letter.
                Nothing is written.
        """
        previous = self._existing(name)
        record = super().write(name, code)
        record.status = "disabled"
        self._upsert(record)
        record_overwrite(
            previous,
            name,
            event_type=CapabilityActivated,
            activations=self.activations,
            revoke=lambda overwritten: demote_capability(self, overwritten, via="agent"),
        )
        return record

    def activate(self, name: str) -> bool:
        """Flip `name` active and record the act from `cli`.

        Args:
            name: The capability's name.

        Returns:
            Whether a capability had that name.

        Raises:
            ValueError: The capability's last write failed validation. Activating broken code is
                refused, with the stored error in the message.
        """
        record = self._existing(name)
        if record is not None and record.last_error is not None:
            msg = f"cannot activate capability {name!r}: it failed validation: {record.last_error}"
            raise ValueError(msg)
        return self._set_status(name, "active", action="activate", via="cli")

    def deactivate(self, name: str) -> bool:
        """Flip `name` disabled, demote it when it was promoted, and record both from `cli`.

        Args:
            name: The capability's name.

        Returns:
            Whether a capability had that name.
        """
        found = self._set_status(name, "disabled", action="deactivate", via="cli")
        if found:
            demote_capability(self, name, via="cli")
        return found

    def disable(self, name: str) -> bool:
        """The agent's own `disable_custom_capability` tool: disable `name` and record it from `agent`.

        It records the same deactivation `deactivate` does, and demotes `name` when it was promoted, so
        the off switch revokes a promotion rather than leaving it live while the manifest says disabled.

        Args:
            name: The capability's name.

        Returns:
            Whether a capability had that name.
        """
        found = super().disable(name)
        if found:
            self.activations.record(CapabilityActivated(name=name, action="deactivate", via="agent"))
            demote_capability(self, name, via="agent")
        return found

    def delete(self, name: str) -> bool:
        """Remove `name`'s manifest entry and its module file, and record the act from `cli`.

        An active capability is refused, so tidying the library never changes the setup. A disabled
        capability is never promoted, because deactivating demotes it.

        Args:
            name: The capability's name.

        Returns:
            Whether a capability had that name.

        Raises:
            ValueError: `name` is still active. It has to be disabled first.
        """
        record = self._existing(name)
        if record is None:
            return False
        if record.status == "active":
            msg = f"cannot delete capability {name!r} while it is active: disable it first"
            raise ValueError(msg)
        manifest = self._load_manifest()
        manifest.capabilities = [entry for entry in manifest.capabilities if entry.name != name]
        self._save_manifest(manifest)
        (self.directory / record.module_file).unlink(missing_ok=True)
        self.activations.record(CapabilityActivated(name=name, action="delete", via="cli"))
        return True

    def _set_status(
        self, name: str, status: Literal["active", "disabled"], *, action: Literal["activate", "deactivate"], via: Literal["cli", "agent"]
    ) -> bool:
        manifest = self._load_manifest()
        found = False
        for record in manifest.capabilities:
            if record.name == name:
                record.status = status
                found = True
        if found:
            self._save_manifest(manifest)
            self.activations.record(CapabilityActivated(name=name, action=action, via=via))
        return found
