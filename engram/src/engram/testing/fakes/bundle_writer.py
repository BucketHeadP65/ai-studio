"""An in-memory `BundleWriter` for unit tests that never touches the filesystem.

:class:`RecordingBundleWriter` records each export and its ``out_dir`` handed to
:meth:`write_bundle` instead of writing to disk. Its state is public: `bundles`, ``out_dirs``
and the :meth:`files` view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.export.contracts import OkfBundle


class RecordingBundleWriter:
    """Records each :meth:`write_bundle` call without writing files (a :class:`BundleWriter`)."""

    def __init__(self) -> None:
        self.bundles: list[OkfBundle] = []
        self.out_dirs: list[str] = []

    def write_bundle(self, bundle: OkfBundle, out_dir: str) -> None:
        """Record the export and its destination instead of writing files.

        Args:
            bundle: The export to record.
            out_dir: The destination directory to record.
        """
        self.bundles.append(bundle)
        self.out_dirs.append(out_dir)

    def files(self) -> dict[str, str]:
        """Map each file of the last recorded export to its body.

        Returns:
            Each concept path, and the root `index.md`, mapped to its body. Empty when nothing was written.
        """
        if not self.bundles:
            return {}
        bundle = self.bundles[-1]
        return {concept.path: concept.body for concept in (*bundle.concepts, bundle.index)}
