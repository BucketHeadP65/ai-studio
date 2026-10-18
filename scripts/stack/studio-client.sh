#!/usr/bin/env bash
# The dashboard dev server on :3031 (hot reload on purpose -- frontend changes go live
# without a restart).
# Foreground, in this shell: launchd owns the process and restarts it on exit.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT/studio-client"

exec npm run dev
