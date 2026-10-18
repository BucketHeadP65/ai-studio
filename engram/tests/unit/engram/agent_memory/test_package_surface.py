"""The agent-memory package exposes nothing at package level and loads none of its modules on import."""

import subprocess
import sys


def test_importing_the_package_loads_no_submodule_and_exposes_no_name() -> None:
    probe = (
        "import sys, engram.agent_memory as package\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('engram.agent_memory.'))\n"
        "public = sorted(name for name in vars(package) if not name.startswith('_'))\n"
        "print(loaded, public)\n"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)  # noqa: S603

    assert result.stdout.strip() == "[] []"
