"""A session pool holding what a test put in it, and the late seam a tool reaches it through."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.session import PooledSessions, SessionPool, TurnHold, _Slot

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai_harness.compaction import ContextUsage

    from harness.features.sessions.contracts import Sessions
    from harness.session import Runtime


class FakeSessionPool:
    """Holds the runtimes a test put in it, keyed by session id, with one as the default."""

    def __init__(self, default: Runtime, *, held: dict[str, Runtime] | None = None) -> None:
        self.default = default
        self._held = dict(held or {})
        self.dropped: list[str] = []
        self.created: list[Path | None] = []
        self.interrupted: list[str] = []
        self.evictions = 0

    async def get(self, session_id: str) -> Runtime:
        """The runtime under `session_id`, the default when nothing is held under it."""
        return self._held.get(session_id, self.default)

    async def create(self, project_root: Path | None, *, session_kind: str = "chat") -> Runtime:
        """Record the request and hand back the default runtime, as though a fresh one opened on it."""
        del session_kind
        self.created.append(project_root)
        return self.default

    async def begin_turn(self, session_id: str) -> TurnHold:
        """Hold whatever runtime `session_id` names, the default when nothing is held under it."""
        return TurnHold(_Slot(runtime=self._held.get(session_id, self.default), touched=0.0))

    async def begin_default_turn(self) -> TurnHold:
        """Hold the default runtime for the turn about to run."""
        return TurnHold(_Slot(runtime=self.default, touched=0.0))

    def interrupt(self, session_id: str) -> bool:
        """Record the interrupt and say there was nothing in flight to cancel."""
        self.interrupted.append(session_id)
        return False

    def steering(self, session_id: str) -> Runtime | None:
        """The runtime for `session_id`, when a test is holding one under it, else `None`."""
        return self._held.get(session_id)

    def held_sessions(self) -> tuple[Runtime, ...]:
        """Every runtime this pool holds."""
        return (self.default, *self._held.values())

    def project_id_of(self, runtime: Runtime) -> str:
        """`""`, the same as a pool built with no project index."""
        del runtime
        return ""

    async def root_of_project(self, project_id: str) -> Path | None:
        """`None`, the same as a pool built with no project index."""
        del project_id
        return None

    def is_answering(self, session_id: str) -> bool:
        """Whether a turn is in flight in that session."""
        del session_id
        return False

    def context_of(self, session_id: str) -> ContextUsage | None:
        """`None`, the same as a session this pool holds no context reading for."""
        del session_id
        return None

    def drop(self, session_id: str) -> bool:
        """Record the drop."""
        self.dropped.append(session_id)
        return True

    def evict_idle(self, *, older_than_minutes: int = 30) -> None:
        """Record the sweep. Drops nothing: a test's held sessions stay held under it."""
        del older_than_minutes
        self.evictions += 1


class APool:
    """`PooledSessions` over a pool a test already built, answered without a container."""

    def __init__(self, pool: SessionPool) -> None:
        """Answer `pool` whenever something asks."""
        self._pool = pool

    async def pool(self) -> SessionPool:
        """The pool this test built."""
        return self._pool


_proof: type[Sessions] = FakeSessionPool
_held_proof: type[PooledSessions] = APool
"""Pyright fails this module the moment `FakeSessionPool` drifts from the `Sessions` port."""
