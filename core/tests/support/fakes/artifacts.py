from __future__ import annotations

from dataclasses import dataclass, field

from core.ai.artifacts import ArtifactDocument, ArtifactRef, ArtifactUnavailableError


@dataclass
class FakeArtifactResolver:
    """A scriptable resolver: answers with a document by ref id, refuses one held as expired."""

    documents: dict[str, ArtifactDocument] = field(default_factory=dict)
    expired: frozenset[str] = frozenset()
    calls: list[ArtifactRef] = field(default_factory=list)

    async def resolve(self, ref: ArtifactRef) -> ArtifactDocument:
        self.calls.append(ref)
        if ref.id in self.expired:
            msg = f"artifact {ref.id!r} is no longer available"
            raise ArtifactUnavailableError(msg)
        document = self.documents.get(ref.id)
        if document is None:
            msg = f"artifact {ref.id!r} is unknown to this resolver"
            raise ArtifactUnavailableError(msg)
        return document
