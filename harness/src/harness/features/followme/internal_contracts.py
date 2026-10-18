"""The ports the console acts call: the agent settings, the run controls and the workflow cards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Tier


class SettingsActs(Protocol):
    """The agent-composition settings, each method answering the sentence to tell the model.

    A refusal is a sentence too.
    """

    async def set_model(self, model_id: str, tier: Tier = "orchestrator") -> str:
        """Switch the model a tier answers with.

        Args:
            model_id: The id of the model the tier answers with from now on.
            tier: The tier to switch. The orchestrator when not given.

        Returns:
            The sentence to relay.
        """
        ...

    async def select_profile(self, name: str | None) -> str:
        """Put a profile in effect, or clear the one in effect.

        Args:
            name: The profile to put in effect, or `None` to clear it.

        Returns:
            The sentence to relay.
        """
        ...

    async def toggle_plugin(self, name: str, *, on: bool) -> str:
        """Turn one plugin on or off.

        Args:
            name: The plugin's name.
            on: Whether the plugin is turned on.

        Returns:
            The sentence to relay.
        """
        ...


class RunActs(Protocol):
    """The fleet buttons over a session's live run, and the gate decision, addressed by session id.

    Each method answers the sentence to tell the model. A refusal is a sentence too.
    """

    async def pause(self, session_id: str) -> str:
        """Pause the run in this session.

        Args:
            session_id: The id of the session whose run is paused.

        Returns:
            The sentence to relay.
        """
        ...

    async def resume(self, session_id: str) -> str:
        """Resume the run in this session.

        Args:
            session_id: The id of the session whose run is resumed.

        Returns:
            The sentence to relay.
        """
        ...

    async def stop(self, session_id: str) -> str:
        """Stop the run in this session.

        Args:
            session_id: The id of the session whose run is stopped.

        Returns:
            The sentence to relay.
        """
        ...

    async def set_automode(self, session_id: str, *, on: bool) -> str:
        """Turn this session's automode on or off.

        Args:
            session_id: The id of the session whose automode is set.
            on: Whether automode is turned on.

        Returns:
            The sentence to relay.
        """
        ...

    async def decide(self, session_id: str, *, approve: bool, feedback: str) -> str:
        """Decide the gate waiting in this session.

        Args:
            session_id: The id of the session whose gate is decided.
            approve: Whether the gate is approved.
            feedback: The words a rejection passes to the rerun. It may be blank.

        Returns:
            The sentence to relay.
        """
        ...


class CardActs(Protocol):
    """Upkeep of the workflows in the library: archive, restore, and the card's own words.

    Each method answers the sentence to tell the model. A refusal is a sentence too.
    """

    async def archive(self, workflow_id: str) -> str:
        """Take the workflow out of the library.

        Args:
            workflow_id: The id of the workflow to archive.

        Returns:
            The sentence to relay.
        """
        ...

    async def restore(self, workflow_id: str) -> str:
        """Put the archived workflow back in the library.

        Args:
            workflow_id: The id of the archived workflow.

        Returns:
            The sentence to relay.
        """
        ...

    async def redescribe(self, workflow_id: str, *, title: str, summary: str, applicability: str) -> str:
        """Rewrite the workflow's card.

        A blank `summary` or `applicability` keeps the card's current one.

        Args:
            workflow_id: The id of the workflow whose card is rewritten.
            title: The card's new title.
            summary: The card's new one-line summary.
            applicability: The card's new words on when the workflow applies.

        Returns:
            The sentence to relay.
        """
        ...
