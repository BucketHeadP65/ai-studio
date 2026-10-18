"""The agent's filesystem anchors, pinned against the parent counts that compute them.

`CHECKOUT` walks a fixed number of parents up from `environment.py`, and everything else here is
derived from it: the shell's root, the spill directory, the sibling engram checkout, the knowledge base
settings file, the repo-docs knowledge_base_schema. A module that moves one level changes all of them at
once, to paths that plausibly exist — so the failure is a shell confined to the wrong tree, not
an error anybody sees.

Shapes rather than absolute paths, so the checks hold on any clone. `application_settings.json`
itself is gitignored and machine-local, so only its *path* is asserted; `REPO_DOCS_SCHEMA`
names a committed file, resolved eagerly on every `embedded_read_tools` open (including the agent's
own startup lifespan), so its existence is asserted directly.
"""

from __future__ import annotations

import tomllib

from pathlib import Path

from harness.settings import CHECKOUT, ENGRAM_REPO, ENGRAM_SETTINGS, REPO_DOCS_SCHEMA, WORKSPACE, repo_path


def test_the_checkout_anchor_is_this_projects_own_root() -> None:
    """`CHECKOUT` names the directory whose `pyproject.toml` declares this very project."""
    manifest = CHECKOUT / "pyproject.toml"

    assert CHECKOUT.is_dir()
    assert manifest.is_file()
    assert tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["name"] == "harness"


def test_the_engram_anchor_is_the_sibling_checkout() -> None:
    """The knowledge base checkout is found beside this one, not at an absolute home path."""
    assert ENGRAM_REPO.is_dir()
    assert ENGRAM_REPO.parent == CHECKOUT.parent


def test_the_settings_path_names_the_engram_checkouts_config_file() -> None:
    """The knowledge base settings file is addressed as `engram/config/application_settings.json`."""
    assert ENGRAM_SETTINGS.parts[-3:] == ("engram", "config", "application_settings.json")


def test_the_repo_docs_knowledge_base_schema_anchor_names_a_real_file() -> None:
    """A stale path here crashes the agent's boot lifespan, not a request handler — no try/except catches it."""
    assert REPO_DOCS_SCHEMA.is_file()


def test_a_repo_override_names_the_checkout(tmp_path: Path) -> None:
    """`APP_ENGRAM_REPO` / `APP_STUDIO_REPO` beat the sibling default, user-expanded."""
    environ = {"APP_TEST_REPO": str(tmp_path / "elsewhere")}

    assert repo_path("APP_TEST_REPO", Path("/sibling/default"), environ=environ) == tmp_path / "elsewhere"


def test_without_an_override_the_sibling_default_stands() -> None:
    assert repo_path("APP_TEST_REPO", Path("/sibling/default"), environ={}) == Path("/sibling/default")


def test_an_empty_override_is_no_override() -> None:
    """`APP_STUDIO_REPO=` with nothing after it is an unfilled export, not a path."""
    assert repo_path("APP_TEST_REPO", Path("/sibling/default"), environ={"APP_TEST_REPO": ""}) == Path("/sibling/default")


def test_the_workspace_anchor_defaults_to_the_checkouts_parent() -> None:
    """`WORKSPACE` is where the agent's shell and files plugins work: the repo root by default."""
    assert CHECKOUT.parent == WORKSPACE


def test_a_workspace_override_names_the_checkout(tmp_path: Path) -> None:
    """`APP_WORKSPACE` beats the checkout's-parent default, the same override `WORKSPACE` itself resolves through."""
    environ = {"APP_WORKSPACE": str(tmp_path / "elsewhere")}

    assert repo_path("APP_WORKSPACE", CHECKOUT.parent, environ=environ) == tmp_path / "elsewhere"
