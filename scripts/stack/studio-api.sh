#!/usr/bin/env bash
# The corpus API on 127.0.0.1:8100.
# Foreground, in this shell: launchd owns the process and restarts it on exit.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT/studio"

# Print the log records of `serve-http` and of the `studio worker` child it starts into this service's
# log, a refresh's scan warnings included. Only this wrapper turns it on: `studio mcp` reads the same
# settings file and speaks over stdout, so the file leaves `logging.console` off.
export APP_LOGGING__CONSOLE=true

exec uv run studio serve-http
