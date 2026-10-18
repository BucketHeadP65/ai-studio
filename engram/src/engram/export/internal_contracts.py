"""The export slice's ports: the writer of the export directory and the reader of source links."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.export.domain.models import OkfBundle

__all__ = ["BundleWriter", "SourceLinkReader"]


@runtime_checkable
class BundleWriter(Protocol):
    """Writes a projected `OkfBundle` to an export directory.

    The writer joins paths and creates every folder it needs.
    """

    def write_bundle(self, bundle: OkfBundle, out_dir: str) -> None:
        """Write every concept and the root `index.md` under `out_dir`.

        Args:
            bundle: The projected concepts and root index.
            out_dir: The root of the export directory.
        """
        ...


@runtime_checkable
class SourceLinkReader(Protocol):
    """Reads the deep link every mirrored source carries, keyed by that source's own key.

    A page naming a source under the schema's `sources.key_field` gets that source's link as the
    `resource` of its OKF `sources` entry.
    """

    async def load_source_links(self) -> Mapping[str, str]:
        """Map each mirrored source's key to its deep link.

        Returns:
            Each source key with its deep link. A source that carries no link is absent.
        """
        ...
