r"""Unit tests for the shared DB-URL resolver and the `init all --embed` CLI surface.

Pinned here:

* `resolve_db_url` is a real public cross-module seam (no leading underscore),
  exported from `engram.init.contracts` / `engram.init.infrastructure.db`; there is no `_resolve_db_url`
  underscore alias.
* `resolve_db_url(override)` is the single DB-URL precedence ladder
  (`override` -> `APP_INFRASTRUCTURE__DATABASE__URL` -> JSON `infrastructure.database.url` ->
  raise) that both `studio.knowledge_base.cli_init` and `alembic/env.py:_resolve_url` consume, so the
  precedence rule lives in exactly one place.
* `init all` exposes `--embed` (default off), not the `--no-embed` /
  `--no-no-embed` double-negative.

The alembic-side delegation (env.py's `_resolve_url` calling `resolve_db_url`) is
covered end-to-end by the integration migration suite (`tests/integration/test_migrations.py`),
which drives `alembic upgrade head` through the `config.attributes["sqlalchemy_url"]` seam.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from typer.testing import CliRunner

from studio.knowledge_base.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


# ---------------------------------------------------------------------------
# the public name is the contract
# ---------------------------------------------------------------------------


class TestPublicResolverName:
    """`resolve_db_url` is the public name; there is no underscore alias."""

    def test_resolve_db_url_is_importable_from_the_feature_package(self) -> None:
        from engram.init.contracts import resolve_db_url

        assert callable(resolve_db_url)

    def test_resolve_db_url_is_importable_from_the_db_module(self) -> None:
        from engram.init.infrastructure.db import resolve_db_url

        assert callable(resolve_db_url)

    def test_underscore_alias_is_gone(self) -> None:
        import engram.init.infrastructure.db as db_mod

        assert not hasattr(db_mod, "_resolve_db_url"), "the private _resolve_db_url alias must be dropped (F57)"

    def test_all_exports_the_public_name_not_the_underscore(self) -> None:
        import engram.init.contracts as init_pkg

        assert "resolve_db_url" in init_pkg.__all__
        assert "_resolve_db_url" not in init_pkg.__all__

    def test_config_value_stays_private(self) -> None:
        # Only the re-exported seam is public; `_config_value` is genuinely module-private.
        import engram.init.contracts as init_pkg

        assert "_config_value" not in init_pkg.__all__


# ---------------------------------------------------------------------------
# one shared precedence ladder: override -> env -> JSON -> raise
# ---------------------------------------------------------------------------


class TestResolveDbUrlPrecedence:
    """`resolve_db_url(override)` is the single DB-URL precedence resolver."""

    def test_override_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engram.init.infrastructure.db import resolve_db_url

        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://env/db")
        assert resolve_db_url("postgresql+asyncpg://explicit/db") == "postgresql+asyncpg://explicit/db"

    def test_a_libpq_spelled_url_is_translated_for_the_async_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engram.init.infrastructure.db import resolve_db_url

        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://env/db?sslmode=disable")
        assert resolve_db_url(None) == "postgresql+asyncpg://env/db", (
            "every consumer of this ladder feeds an asyncpg engine, which refuses libpq's sslmode word"
        )

    def test_env_wins_over_json_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from engram.init.infrastructure.db import resolve_db_url

        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", "postgresql+asyncpg://env/db")
        assert resolve_db_url(None) == "postgresql+asyncpg://env/db"

    def test_json_fallback_when_no_override_and_no_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import engram.init.infrastructure.db as db_mod

        monkeypatch.delenv("APP_INFRASTRUCTURE__DATABASE__URL", raising=False)
        json_cfg = tmp_path / "application_settings.json"
        json_cfg.write_text(json.dumps({"infrastructure": {"database": {"url": "postgresql+asyncpg://json/db"}}}), encoding="utf-8")
        monkeypatch.setattr(db_mod, "_CONFIG_JSON", json_cfg)

        assert db_mod.resolve_db_url(None) == "postgresql+asyncpg://json/db"

    def test_raises_when_nothing_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import engram.init.infrastructure.db as db_mod

        monkeypatch.delenv("APP_INFRASTRUCTURE__DATABASE__URL", raising=False)
        monkeypatch.setattr(db_mod, "_CONFIG_JSON", tmp_path / "nonexistent.json")

        with pytest.raises(ValueError, match="No database URL"):
            db_mod.resolve_db_url(None)


class TestAlembicEnvDelegates:
    """`alembic/env.py:_resolve_url` delegates to the shared `resolve_db_url` (F58, no drift)."""

    def test_env_resolve_url_uses_the_shared_resolver(self) -> None:
        # The env.py module imports the shared resolver rather than re-implementing the ladder.
        import ast
        import pathlib

        # The one alembic chain is engram's, in the engram checkout beside this one: `parents[4]`
        # is the monorepo root, whose `engram/` sibling holds it.
        env_path = pathlib.Path(__file__).resolve().parents[4] / "engram" / "alembic" / "env.py"
        env_src = env_path.read_text(encoding="utf-8")
        tree = ast.parse(env_src)
        imports_shared = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "engram.init.contracts"
            and any(alias.name == "resolve_db_url" for alias in node.names)
            for node in ast.walk(tree)
        )
        assert imports_shared, "alembic/env.py must import resolve_db_url from engram.init.contracts (F58)"


# ---------------------------------------------------------------------------
# the --embed flag replaces the --no-embed double-negative
# ---------------------------------------------------------------------------


class TestInitAllEmbedFlag:
    """`init all` exposes `--embed` defaulting off, killing the `--no-no-embed` double-negative."""

    def test_help_advertises_embed_defaulting_off(self) -> None:
        # `embed: bool = False` renders the clean `--embed / --no-embed` pair defaulting to
        # no-embed, rather than a `no_embed: bool = True` whose pair is `--no-embed / --no-no-embed`.
        result = runner.invoke(app, ["init", "all", "--help"])

        assert result.exit_code == 0, result.output
        assert "--embed" in result.output
        assert "no-embed" in result.output  # the default is rendered as [default: no-embed]

    def test_no_double_negative_flag(self) -> None:
        # The awkward double-negative the finding targets must be gone.
        result = runner.invoke(app, ["init", "all", "--help"])

        assert result.exit_code == 0, result.output
        assert "--no-no-embed" not in result.output
