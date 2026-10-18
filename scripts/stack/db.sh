#!/usr/bin/env bash
# The corpus/workflow Postgres (host port 5434) via engram's compose file.
# Foreground, in this shell: launchd owns the process and restarts it on exit.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT/engram"

# --no-recreate: attach to an already-running, healthy container instead of
# recreating it. A SIGTERM (stop/restart) stops the container; the next start
# brings the same container back up with its volume intact.
exec docker compose up --no-recreate
