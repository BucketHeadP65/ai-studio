# pydantic-graph 2.66.0, patched

This folder is pydantic-graph 2.66.0 from PyPI with one change: the `pydantic_graph/graph_builder.py` part of the open
pull request pydantic/pydantic-ai#10426, with its `from collections.abc import` line merged by hand. Every other file
under `pydantic_graph/` is the 2.66.0 wheel's, unchanged. `LICENSE` is the package's own MIT license.

## Why

In 2.66.0 a join can fire before every branch that feeds it has finished. The node behind the join runs on part of its
items and runs again later on the rest, with no error from the graph. The pull request fixes both shapes this
repository hit, recorded in `docs/explanation/upstream-bugs.md`:

- UB-17: a join finalized in the same pass as another join fires before that join's new task can feed it.
- UB-22: a join behind a map over an empty list fires early when another branch of the same fork finishes first.

`core/tests/unit/workflows/test_join_waits_for_every_branch.py` runs both repros. It fails on stock 2.66.0 and passes
on this copy.

## How the projects get it

`core/pyproject.toml` declares `pydantic-graph==2.66.0` as a main dependency with an editable path source to this
folder. engram, studio, harness and connectors depend on core by path, and uv applies core's sources in their locks
too. All five locks hold `source = { editable = "../vendor/pydantic-graph" }`. The version stays 2.66.0 because
pydantic-ai-slim 2.66.0 pins `pydantic-graph==2.66.0`. The same pin means uv cannot take a pydantic-ai release newer
than 2.66.0 while this folder is in use.

A change to a file here is a backend change: restart the stack with `scripts/stack/stack.sh restart harness studio-api`.

## When to delete it

Delete it once a pydantic-graph release contains pydantic/pydantic-ai#10426. Then:

1. In `core/pyproject.toml`, remove the `pydantic-graph` entry under `[tool.uv.sources]`, raise the `pydantic-graph`
   requirement to that release, and rewrite its comment.
2. Run `uv lock` and `uv sync` in core, engram, studio, harness and connectors. Each lock's `pydantic-graph` entry must
   say `source = { registry = "https://pypi.org/simple" }`.
3. Delete this folder, remove every mention of it (`git grep -n vendor/pydantic-graph` finds them, the root
   `CLAUDE.md` among them), and update UB-17 and UB-22.
4. Keep the regression test. It must pass on the release too.
