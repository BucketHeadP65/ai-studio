"""The chat agent, served over stdio to any ACP client such as Zed.

The session comes out of the same container the HTTP entry uses, with one difference: `SaveApproval(required=True)`,
so every `save_workflow` call becomes a permission request the person in the editor answers.

An ACP client spawns this entry as its own process, apart from the HTTP server. Both entries share this deployment's
on-disk state (transcripts, run log, evidence log), so run one conversation at a time across the two.

    uv run python -m harness.acp     # then point an ACP client (e.g. Zed's agent_servers) at it
"""

from __future__ import annotations

import asyncio
import warnings

from core.observability import stop_tracing
from core.settings import InfrastructureSettings
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.experimental import HarnessExperimentalWarning

from harness.container import create_container
from harness.features.authoring.contracts import CHAT_BUDGET
from harness.features.authoring.wiring import SaveApproval
from harness.features.running.infrastructure.acp_gate import AcpGateAsker
from harness.session import Runtime, answerable
from harness.settings import HarnessSettings
from harness.tracing import LogfireTracing

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)


async def main() -> None:
    """Build the session, then serve its agent on stdio until the client hangs up."""
    from pydantic_ai_harness.experimental.acp import AcpSession, AcpSessionConfig, run_acp_stdio  # noqa: PLC0415 (the module warns on import, so it follows the filter above)

    settings = HarnessSettings()
    container = create_container(settings)
    try:
        LogfireTracing((await container.get(InfrastructureSettings)).logfire, settings.logging).start()
        # Every save asks the editor first. The session is a singleton, so resolving it inside this block
        # makes the override hold for the whole process.
        with container.override({SaveApproval: SaveApproval(required=True)}):
            session = await container.get(Runtime)

        def configure(acp_session: AcpSession) -> AcpSessionConfig[Runtime]:
            # This process serves no /api/decide, so a gate asks through the editor's permission prompt.
            # One session serves consecutive editor sessions, and the newest one answers the gates.
            session.gate_asker = AcpGateAsker(client=acp_session.client, session_id=acp_session.session_id)
            return AcpSessionConfig(deps=session)

        # The same per-turn budget the HTTP entry passes, so `WarnNearLimits(max_iterations=CHAT_BUDGET)`
        # warns before the real limit. A gated verb ends the turn deferred, and `run_acp_stdio` asks the
        # editor through `session/request_permission`. `answerable` offers the model the verbs that wait for the person.
        with answerable():
            await run_acp_stdio(
                session.agent,
                deps=session,
                name="harness",
                session_config=configure,
                usage_limits=UsageLimits(request_limit=CHAT_BUDGET),
            )
    finally:
        await container.close()
        stop_tracing()


if __name__ == "__main__":
    asyncio.run(main())
