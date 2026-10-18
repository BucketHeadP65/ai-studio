"""Drive another ACP agent from Python: the client half of the Agent Client Protocol.

`pydantic_ai_harness.experimental.acp` serves a Pydantic AI agent to an editor. This package is the
other direction: [`ExternalAgent`][harness.adapters.acp_client.ExternalAgent]
spawns an ACP agent (Claude Code, Codex, and the rest) and runs one prompt turn on it.
"""

from harness.adapters.acp_client._external import AgentUpdate, ExternalAgent, ExternalResult, UpdateSink
from harness.adapters.acp_client._permission import AllowOnce, PermissionPolicy, outcome_for

__all__ = ["AgentUpdate", "AllowOnce", "ExternalAgent", "ExternalResult", "PermissionPolicy", "UpdateSink", "outcome_for"]
