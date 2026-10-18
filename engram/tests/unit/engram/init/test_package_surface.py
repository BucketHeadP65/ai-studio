"""The init package exposes nothing at package level, and its non-database names load no alembic."""

import subprocess
import sys


def test_importing_the_package_loads_no_submodule_and_exposes_no_name() -> None:
    probe = (
        "import sys, engram.init as package\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('engram.init.'))\n"
        "public = sorted(name for name in vars(package) if not name.startswith('_'))\n"
        "print(loaded, public)\n"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert result.stdout.strip() == "[] []"


def test_asking_contracts_for_write_scaffold_loads_neither_alembic_nor_the_database_module() -> None:
    probe = (
        "import sys, engram.init.contracts as contracts\n"
        "contracts.write_scaffold\n"
        "print(sorted(name for name in ('alembic', 'engram.init.infrastructure.db') if name in sys.modules))\n"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert result.stdout.strip() == "[]"
