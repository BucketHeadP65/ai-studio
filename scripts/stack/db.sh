#!/usr/bin/env bash
# The corpus/workflow Postgres (host port 5434) via engram's compose file.
# Foreground, in this shell: launchd owns the process and restarts it on exit.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT/engram"

# Docker Desktop can quit while the stack runs (it has quit when a USB drive or dock was unplugged).
# Start it again and wait up to two minutes for its engine, so the database comes back without a person.
# If the engine never answers, exit and let launchd try again.
if ! docker info >/dev/null 2>&1; then
  echo "db: the Docker engine is not answering, starting Docker Desktop"
  open -a Docker || true
  for _ in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || { echo "db: the Docker engine did not start within two minutes"; exit 1; }
fi

# --no-recreate: attach to an already-running, healthy container instead of
# recreating it. A SIGTERM (stop/restart) stops the container; the next start
# brings the same container back up with its volume intact.
exec docker compose up --no-recreate
