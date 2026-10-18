"""The custom steps on disk: one Python module per step, each landing disabled until the owner activates it.

Validation is registration. Every public async function a written module defines is registered on a
throwaway `StepRegistry().copy()`, so the engine's own checks (async only, every parameter and the
return annotated, `Any` refused) are the validator, and their error text is the refusal the agent
reads, verbatim.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import RegistrationError, StepRegistry
from core.files import atomic_write
from pydantic import BaseModel, ValidationError

from harness.features.selfauthoring.domain import CustomStep, StepActivated
from harness.features.selfauthoring.exceptions import StepValidationError
from harness.features.selfauthoring.infrastructure.activation_log import NoActivations, record_overwrite

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import ModuleType

    from harness.features.selfauthoring.internal_contracts import ActivationLog

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_module(path: Path) -> ModuleType:
    """Import `path` as a fresh module outside `sys.modules`, with no bytecode cache, so a rewrite always runs the new source."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        msg = f"cannot create an import spec for {path.name}"
        raise StepValidationError(msg)
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        import_msg = f"{type(exc).__name__}: {exc}"
        raise StepValidationError(import_msg) from exc
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
    return module


def _public_async_functions(module: ModuleType) -> list[Callable[..., Awaitable[object]]]:
    """Every public async function `module` defines itself, in name order. An empty list is what refuses a module."""
    found: list[Callable[..., Awaitable[object]]] = []
    for attr_name in sorted(dir(module)):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name)
        if inspect.iscoroutinefunction(obj) and getattr(obj, "__module__", None) == module.__name__:
            found.append(obj)
    return found


def validate_step_file(path: Path) -> tuple[str, ...]:
    """Import `path`, then register every public async function it defines into a throwaway registry of steps.

    Only functions the module defines itself count. An imported `async def`, a sync function and an
    underscore-prefixed function are not steps. The registry's own error message is re-raised verbatim.

    Args:
        path: The step module.

    Returns:
        The registered function names, sorted.

    Raises:
        StepValidationError: The module does not import, defines no public async function, or a
            function fails the registry's own checks.
    """
    module = _load_module(path)
    functions = _public_async_functions(module)
    if not functions:
        msg = "a step module must define at least one public async function"
        raise StepValidationError(msg)
    validator = StepRegistry().copy()
    try:
        for fn in functions:
            validator.register(fn)
    except RegistrationError as exc:
        raise StepValidationError(str(exc)) from exc
    return tuple(sorted(fn.__name__ for fn in functions))


def load_step_functions(path: Path) -> list[Callable[..., Awaitable[object]]]:
    """Re-import `path` and return every public async function it defines.

    Args:
        path: The step module.

    Returns:
        The functions, in name order.

    Raises:
        StepValidationError: The module no longer imports.
    """
    return _public_async_functions(_load_module(path))


class _Manifest(BaseModel):
    steps: list[CustomStep] = []


@dataclass
class FileCustomSteps:
    """The custom steps on disk, each landing disabled until the owner activates it.

    Each custom step is one `<name>.py` file under `directory`, indexed by a sibling `manifest.json`.
    Pending is the only landing state, even for a step that validates cleanly. Only `activate` and
    `deactivate`, driven by the owner, flip an entry, and each act is recorded in the activation log.
    """

    directory: Path
    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded. Custom steps that are only read are built
    over `NoActivations`."""

    @classmethod
    def read_only(cls, directory: Path) -> FileCustomSteps:
        """Custom steps over `directory` that record nowhere, for a caller that only reads.

        Args:
            directory: The folder holding the step modules and the manifest.

        Returns:
            The custom steps, built over `NoActivations`.
        """
        return cls(directory, activations=NoActivations())

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

    def _upsert(self, record: CustomStep) -> None:
        manifest = self._load_manifest()
        for index, existing in enumerate(manifest.steps):
            if existing.name == record.name:
                manifest.steps[index] = record
                break
        else:
            manifest.steps.append(record)
        self._save_manifest(manifest)

    def _existing(self, name: str) -> CustomStep | None:
        for record in self.list_all():
            if record.name == name:
                return record
        return None

    def write(self, name: str, code: str) -> CustomStep:
        """Write `code` to `<name>.py`, validate it, and upsert its manifest entry disabled.

        A step that validates cleanly lands disabled exactly like one that failed validation. The owner
        activates it with `harness activate step <name>`. Overwriting an active step is itself a
        deactivation, recorded from `agent`, the only surface that writes.

        Args:
            name: The name to write the step module under.
            code: The complete Python source.

        Returns:
            The manifest entry as written, with the validation error when there was one.

        Raises:
            ValueError: `name` is not lowercase letters, digits and underscores starting with a letter.
                Nothing is written.
        """
        if not _NAME_RE.match(name):
            msg = f"invalid step name {name!r}: use lowercase letters, digits, and underscores, starting with a letter"
            raise ValueError(msg)
        previous = self._existing(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        try:
            function_names = validate_step_file(path)
            record = CustomStep(name=name, module_file=path.name, function_names=list(function_names))
        except StepValidationError as exc:
            record = CustomStep(name=name, module_file=path.name, function_names=[], last_error=str(exc))
        self._upsert(record)
        record_overwrite(previous, name, event_type=StepActivated, activations=self.activations, revoke=None)
        return record

    def activate(self, name: str) -> bool:
        """Flip `name` active and record the act from `cli`.

        Args:
            name: The step's name.

        Returns:
            Whether a step had that name.

        Raises:
            ValueError: The step's last write failed validation. Activating broken code is refused,
                with the stored error in the message.
        """
        record = self._existing(name)
        if record is not None and record.last_error is not None:
            msg = f"cannot activate step {name!r}: it failed validation: {record.last_error}"
            raise ValueError(msg)
        return self._set_status(name, "active", action="activate")

    def deactivate(self, name: str) -> bool:
        """Flip `name` disabled and record the act from `cli`.

        Args:
            name: The step's name.

        Returns:
            Whether a step had that name.
        """
        return self._set_status(name, "disabled", action="deactivate")

    def _set_status(self, name: str, status: Literal["active", "disabled"], *, action: Literal["activate", "deactivate"]) -> bool:
        manifest = self._load_manifest()
        found = False
        for record in manifest.steps:
            if record.name == name:
                record.status = status
                found = True
        if found:
            self._save_manifest(manifest)
            self.activations.record(StepActivated(name=name, action=action, via="cli"))
        return found

    def delete(self, name: str) -> bool:
        """Remove `name`'s manifest entry and its module file, and record the act from `cli`.

        An active step is refused, so tidying the library never changes the setup.

        Args:
            name: The step's name.

        Returns:
            Whether a step had that name.

        Raises:
            ValueError: `name` is still active. It has to be disabled first.
        """
        record = self._existing(name)
        if record is None:
            return False
        if record.status == "active":
            msg = f"cannot delete step {name!r} while it is active: disable it first"
            raise ValueError(msg)
        manifest = self._load_manifest()
        manifest.steps = [entry for entry in manifest.steps if entry.name != name]
        self._save_manifest(manifest)
        (self.directory / record.module_file).unlink(missing_ok=True)
        self.activations.record(StepActivated(name=name, action="delete", via="cli"))
        return True

    def list_all(self) -> list[CustomStep]:
        """Every manifest entry, in insertion order.

        Returns:
            The entries.
        """
        return self._load_manifest().steps

    def load_active(self, registry: StepRegistry) -> tuple[str, ...]:
        """Register every active step's public async functions into `registry`, in place.

        Each step's whole batch is proven on a throwaway `registry.copy()` first. A step that collides
        with a name `registry` already holds, or one an earlier step in the same call registered, is
        skipped whole, never half-registered, and its error is stored on its entry. One bad step never
        blocks the rest.

        Args:
            registry: The registry of steps to register into.

        Returns:
            The names registered into `registry`, in the order their steps were tried.
        """
        manifest = self._load_manifest()
        loaded: list[str] = []
        changed = False
        for record in manifest.steps:
            if record.status != "active":
                continue
            try:
                functions = load_step_functions(self.directory / record.module_file)
                probe = registry.copy()
                for fn in functions:
                    probe.register(fn)
            except (StepValidationError, RegistrationError) as exc:
                if record.last_error != str(exc):
                    record.last_error = str(exc)
                    changed = True
                continue
            for fn in functions:
                registry.register(fn)
                loaded.append(fn.__name__)
            if record.last_error is not None:
                record.last_error = None
                changed = True
        if changed:
            self._save_manifest(manifest)
        return tuple(loaded)
