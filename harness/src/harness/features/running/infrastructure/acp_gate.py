"""The person gate, wired to an editor's permission prompt instead of a browser panel.

The gate rides `session/request_permission`, the same request a save approval arrives through,
because the editor never reaches `/api/decide`.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from acp import schema, text_block, tool_content

if TYPE_CHECKING:
    from acp import Client

_REVIEW_LIMIT = 4000
"""How many characters of the rendered value the prompt shows. Anything longer is cut, with a note
that the run log holds all of it."""


@dataclass(frozen=True)
class AcpGateAsker:
    """One ACP session's way of asking a gate question, built with the session.

    It offers no "always" option, so every gate reached is answered afresh.
    """

    client: Client
    session_id: str

    async def ask(self, gate_id: str, rendered: str) -> bool:
        """Show `rendered` in the editor as a permission prompt, and read the person's click as the verdict.

        Args:
            gate_id: The gate being asked about.
            rendered: The value under review, rendered for reading. A long value is cut, with a note
                that the run log holds all of it.

        Returns:
            True when the person approved, False when they rejected.

        Raises:
            asyncio.CancelledError: When the editor cancelled the turn while the gate was waiting, which pauses the run.
        """
        shown = rendered if len(rendered) <= _REVIEW_LIMIT else rendered[:_REVIEW_LIMIT] + "\n... (truncated, the run log holds all of it)"
        response = await self.client.request_permission(
            session_id=self.session_id,
            tool_call=schema.ToolCallUpdate(
                # A fresh id, not the in-flight tool call's: updating `run_workflow`'s entry would
                # retitle a running tool to ask a question, and the answer would leave it mislabeled.
                tool_call_id=f"gate:{gate_id}:{uuid4().hex[:8]}",
                title=f"Person gate {gate_id!r}: approve to release this value, reject to rerun its upstream",
                kind="other",
                status="pending",
                content=[tool_content(text_block(shown))],
            ),
            options=[
                schema.PermissionOption(kind="allow_once", name="Approve", option_id="allow_once"),
                schema.PermissionOption(kind="reject_once", name="Reject (rerun upstream)", option_id="reject_once"),
            ],
        )
        outcome = response.outcome
        if isinstance(outcome, schema.DeniedOutcome):
            # ACP signals a cancelled turn through a denied permission, and a cancelled run is a
            # pause rather than a verdict: `CancelledError` rides the arm that keeps the run log
            # and skips the evidence record.
            msg = f"the editor cancelled the turn while gate {gate_id!r} was waiting"
            raise asyncio.CancelledError(msg)
        return outcome.option_id == "allow_once"
