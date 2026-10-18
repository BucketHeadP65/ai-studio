"""`ReadDials`: the read tools' live modes, resolved afresh on every call.

A host that owns the dials hands `RetrievalKnowledgeBase` a `DialsSource`, a callable read on every call.
A dial turned under a running host takes effect on the next tool call, with no rebuild and no re-mount.
A host without one gets the defaults: hybrid search, leveled reads.
The dials never hide a tool. They change how a visible tool behaves.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

SearchDial = Literal["keyword", "hybrid"]
FetchDial = Literal["flat", "leveled"]


class ReadDials(BaseModel, frozen=True):
    """The read tools' live modes.

    `search` can force the keyword-only leg. It can never switch on a vector leg a surface has no
    embedder for. `fetch` chooses between the leveled descent (maps, envelopes, child stubs) and
    flat full-text reads by id (no browsable map).
    """

    search: SearchDial = Field(default="hybrid", description="Which search legs run: keyword-only, or fused with the vector leg.")
    fetch: FetchDial = Field(default="leveled", description="Read shape: flat full text by id, or the leveled descent.")


DialsSource = Callable[[], ReadDials]
"""Answers the dials in effect for the call being made."""

DEFAULT_DIALS = ReadDials()
"""The dials a host without a `DialsSource` gets: hybrid search, leveled reads."""
