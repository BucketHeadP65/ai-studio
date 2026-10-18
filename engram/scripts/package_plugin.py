"""Build a standalone Studio plugin, including the shared skills' actual files."""

# ruff: noqa: INP001, T201 -- standalone packaging command, not an importable package

from __future__ import annotations

import argparse
import shutil

from pathlib import Path
from tempfile import TemporaryDirectory

SOURCE = Path(__file__).resolve().parents[1] / "plugin"
REPOSITORY = SOURCE.parent.parent


def package(destination: Path, *, force: bool = False) -> None:
    """Copy a complete bundle outside the checkout; replacing a bundle is opt-in."""
    destination = destination.expanduser().absolute()
    resolved = destination.resolve()
    if destination.is_symlink() or resolved.name != "knowledge-base":
        msg = "Destination must be a real directory named knowledge-base."
        raise ValueError(msg)
    if resolved.is_relative_to(REPOSITORY) or REPOSITORY.is_relative_to(resolved):
        msg = "Destination must be outside the source checkout."
        raise ValueError(msg)
    if destination.exists() and not force:
        msg = f"{destination} already exists; use --force to replace this generated bundle."
        raise FileExistsError(msg)

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Stage first so missing/broken source files never destroy a working bundle.
    with TemporaryDirectory(prefix=".knowledge-base-package-", dir=destination.parent) as staging:
        bundle = Path(staging) / "knowledge-base"
        shutil.copytree(SOURCE, bundle, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        if destination.exists():
            shutil.rmtree(destination)
        bundle.rename(destination)


def main() -> None:
    """Package the plugin for a local marketplace or release archive."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Output directory named knowledge-base, outside this checkout")
    parser.add_argument("--force", action="store_true", help="Replace an existing generated bundle")
    args = parser.parse_args()
    try:
        package(args.destination, force=args.force)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(args.destination.expanduser().absolute())


if __name__ == "__main__":
    main()
