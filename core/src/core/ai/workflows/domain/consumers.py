"""The consumer record: which saved version of each reusable step a workflow's copy matches, found by content address.

An inline step is a copy of a reusable step when a reusable step of the same name has saved versions. The copy
matches the version whose content address equals its own. A copy that matches none is diverged, and its entry names
the version it diverged from.
"""

from __future__ import annotations

import difflib

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from core.ai.workflows.domain.fingerprint import canonical_step_yaml, step_content_address
from core.ai.workflows.domain.vocabulary import InlineStep

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.domain.vocabulary import Workflow


class StepVersion(BaseModel):
    """One saved version of a reusable step, with its id and content address."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: int
    step_id: str
    """The library id of this version, `st.<name>.v<version>`."""
    step_hash: str
    """The content address of `step`."""
    step: InlineStep


class ConsumerEntry(BaseModel):
    """What one save records about one reusable step the workflow holds a copy of.

    A copy that matches a saved version names it in `version`. A diverged copy has no `version` and names the
    version it diverged from, with how that was found. `diverged_from` and `diverged_basis` come together, and so do
    `declined` and `declined_because`. An entry with no `version` names `diverged_from`, and an entry with a `version`
    does not. An entry that breaks any of these is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    """The reusable step's name, which the copy's name equals."""
    version: int | None
    """The saved version whose content address equals the copy's. None for a diverged copy."""
    step_hash: str
    """The copy's content address."""
    diverged_from: int | None = None
    """The saved version a diverged copy diverged from. None for a copy that matches a version."""
    diverged_basis: Literal["parent", "closest"] | None = None
    """How `diverged_from` was found: `parent` when the parent version's record named it, `closest` when it is the
    saved version closest to the copy by text."""
    declined: str | None = None
    """The content address of a step version this consumer keeps its copy instead of."""
    declined_because: Literal["kept", "reverted"] | None = None
    """Why that version was declined: `kept` when step maintenance kept the copy, `reverted` when a live trial of
    that version reverted."""

    @model_validator(mode="after")
    def _consistent(self) -> ConsumerEntry:
        if (self.diverged_from is None) != (self.diverged_basis is None):
            msg = "diverged_from and diverged_basis come together. Set both or neither"
            raise ValueError(msg)
        if (self.declined is None) != (self.declined_because is None):
            msg = "declined and declined_because come together. Set both or neither"
            raise ValueError(msg)
        if self.version is None and self.diverged_from is None:
            msg = "a copy that matches no saved version names the version it diverged from. Set diverged_from and diverged_basis"
            raise ValueError(msg)
        if self.version is not None and self.diverged_from is not None:
            msg = f"a copy that matches version {self.version} did not diverge. Leave diverged_from and diverged_basis out"
            raise ValueError(msg)
        return self


def consumer_record(
    workflow: Workflow, versions: Mapping[str, Sequence[StepVersion]], *, parent: Sequence[ConsumerEntry] = ()
) -> tuple[ConsumerEntry, ...]:
    """The consumer record of `workflow`: one entry per inline step whose name a reusable step has saved versions of.

    A copy equal to several saved versions names the highest. A diverged copy names the version the parent's entry
    named: its `version`, or the version a diverged parent entry traced to its own parent. Otherwise it names the
    saved version whose canonical YAML is closest to the copy's by line, ties going to the higher version. A
    parent entry's decline carries over while the copy's content address is unchanged, and is dropped once it
    changed. The order of each sequence in `versions` does not matter.

    Args:
        workflow: The workflow being saved.
        versions: Every saved version of each reusable step, by step name. A name absent or mapped to an empty
            sequence has no saved versions.
        parent: The consumer record of the version `workflow` derives from. Empty for a first version.

    Returns:
        The entries, sorted by step name.
    """
    earlier = {entry.step: entry for entry in parent}
    entries: list[ConsumerEntry] = []
    for copy in workflow.inline:
        saved = versions.get(copy.name, ())
        if not saved:
            continue
        address = step_content_address(copy)
        prior = earlier.get(copy.name)
        same_copy = prior if prior is not None and prior.step_hash == address else None
        declined = same_copy.declined if same_copy is not None else None
        declined_because = same_copy.declined_because if same_copy is not None else None
        matching = [saved_version.version for saved_version in saved if saved_version.step_hash == address]
        if matching:
            entries.append(
                ConsumerEntry(
                    step=copy.name, version=max(matching), step_hash=address, declined=declined, declined_because=declined_because
                )
            )
            continue
        diverged_from, basis = _diverged_from(copy, saved, prior)
        entries.append(
            ConsumerEntry(
                step=copy.name,
                version=None,
                step_hash=address,
                diverged_from=diverged_from,
                diverged_basis=basis,
                declined=declined,
                declined_because=declined_because,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.step))


def _diverged_from(copy: InlineStep, saved: Sequence[StepVersion], prior: ConsumerEntry | None) -> tuple[int, Literal["parent", "closest"]]:
    """The version a diverged copy diverged from and how it was found."""
    if prior is not None and prior.version is not None:
        return prior.version, "parent"
    if prior is not None and prior.diverged_from is not None and prior.diverged_basis == "parent":
        return prior.diverged_from, "parent"
    lines = canonical_step_yaml(copy).splitlines()
    closest = max(
        saved,
        key=lambda saved_version: (
            difflib.SequenceMatcher(None, lines, canonical_step_yaml(saved_version.step).splitlines(), autojunk=False).ratio(),
            saved_version.version,
        ),
    )
    return closest.version, "closest"
