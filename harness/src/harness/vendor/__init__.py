"""Third-party source taken verbatim, because the version we need is not released yet.

`loop_detection` is `pydantic-ai-harness` PR #336 (`claude/loop-detection`, open). It is vendored
rather than depended on: that branch is 50 commits behind main and, among other things, predates
the `skills` capability this app's agent is built on, so installing it would trade a loop
guard for a broken agent. The capability itself is three files and imports nothing from the rest of
the harness, so it runs here unchanged -- only `__init__.py`'s intra-package import is rewritten to
this path.

Delete this directory the day the PR merges and import from `pydantic_ai_harness.loop_detection`.
Ruff is configured to skip it (`extend-exclude`) so it stays diffable against upstream.
"""
