"""The slice check holds converted slices to their public modules and keeps I/O in infrastructure."""

import sys
import tempfile
import textwrap
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_slices  # noqa: E402

SLICE = "app.features.orders"


def _messages(files: dict[str, str], *, assembly: frozenset[str] = frozenset(), infrastructure: frozenset[str] = frozenset()) -> list[str]:
    """The messages the check reports for a source tree written to a temporary folder."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        tree = {
            "app/__init__.py": '"""The app."""\n',
            "app/features/__init__.py": '"""The features."""\n',
            "app/features/orders/__init__.py": '"""Orders."""\n',
            "app/features/orders/contracts.py": '"""The public API."""\n',
            "app/features/orders/domain.py": '"""The rules."""\n',
            "app/features/orders/service.py": '"""The use cases."""\n',
            "app/features/orders/infrastructure/__init__.py": '"""Adapters."""\n',
            "app/features/orders/infrastructure/postgres.py": '"""Postgres."""\n',
            "app/features/orders/wiring.py": '"""Registrations."""\n',
            **files,
        }
        for name, text in tree.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        return [finding.message for finding in check_slices.check([root], [SLICE], set(assembly), set(infrastructure))]


class Boundaries(unittest.TestCase):
    """Outside code reaches a converted slice through its contracts only."""

    def test_importing_the_contracts_passes(self) -> None:
        """The contracts module is the public surface."""
        self.assertEqual(_messages({"app/other.py": "from app.features.orders.contracts import Order\n"}), [])

    def test_importing_an_internal_module_is_reported(self) -> None:
        """The service is not public."""
        self.assertEqual(
            _messages({"app/other.py": "from app.features.orders.service import place\n"}),
            ["imports `app.features.orders.service`, only `app.features.orders.contracts` is public"],
        )

    def test_a_relative_import_from_outside_is_resolved(self) -> None:
        """A relative import is checked by where it lands."""
        self.assertEqual(
            _messages({"app/other.py": "from .features.orders import service\n"}),
            ["imports `app.features.orders.service`, only `app.features.orders.contracts` is public"],
        )

    def test_an_assembly_module_may_reach_the_wiring(self) -> None:
        """A composition module assembles the slice."""
        files = {"app/container.py": "from app.features.orders import wiring\n"}
        self.assertEqual(_messages(files, assembly=frozenset({"app.container"})), [])
        self.assertEqual(
            _messages(files),
            ["imports `app.features.orders.wiring`, only `app.features.orders.contracts` is public"],
        )

    def test_the_slice_package_holds_only_its_docstring(self) -> None:
        """A slice's own package re-exports nothing."""
        files = {"app/features/orders/__init__.py": '"""Orders."""\n\nfrom app.features.orders.service import place\n'}
        self.assertEqual(_messages(files), ["`app.features.orders/__init__.py` must hold only its docstring"])


class Inside(unittest.TestCase):
    """Inside a converted slice, the domain stays pure and I/O stays in infrastructure."""

    def test_the_domain_may_not_import_a_database_driver_or_pydantic_ai(self) -> None:
        """Both are forbidden in the domain."""
        files = {"app/features/orders/domain.py": '"""The rules."""\n\nimport asyncpg\nfrom pydantic_ai import Agent\n'}
        self.assertEqual(_messages(files), ["the domain imports `asyncpg`", "the domain imports `pydantic_ai`"])

    def test_the_service_may_not_import_an_http_client(self) -> None:
        """Network access belongs in infrastructure."""
        self.assertEqual(
            _messages({"app/features/orders/service.py": '"""The use cases."""\n\nimport httpx\n'}),
            ["imports `httpx` outside `app.features.orders.infrastructure`"],
        )

    def test_infrastructure_may_import_a_database_driver(self) -> None:
        """Infrastructure is where the driver lives."""
        self.assertEqual(_messages({"app/features/orders/infrastructure/postgres.py": '"""Postgres."""\n\nimport asyncpg\n'}), [])

    def test_a_slice_that_is_infrastructure_may_import_an_http_client_anywhere(self) -> None:
        """A slice named with `--infrastructure` is an application's infrastructure in every module."""
        files = {"app/features/orders/service.py": '"""The use cases."""\n\nimport httpx\n'}
        self.assertEqual(_messages(files, infrastructure=frozenset({SLICE})), [])

    def test_urllib_request_counts_as_a_network_client(self) -> None:
        """The standard library's HTTP client is a network client too."""
        self.assertEqual(
            _messages({"app/features/orders/service.py": '"""The use cases."""\n\nfrom urllib import request\n'}),
            ["imports `urllib.request` outside `app.features.orders.infrastructure`"],
        )


if __name__ == "__main__":
    unittest.main()
