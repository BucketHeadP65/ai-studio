"""Shared pytest fixtures for engram's test suite.

Unit tests need nothing here. The integration tier's containerized-Postgres machinery — the
session-scoped `pg` container, the per-test `migrated` databases, the reader-role URL, and the
`KNOWLEDGE_BASE_TEST_DIALECT` bed selection — ships with the library as the `engram.testing.infrastructure.knowledge_base_db`
plugin, registered below, so every consumer of the library runs the identical bed.
"""

pytest_plugins = ["engram.testing.infrastructure.knowledge_base_db"]
