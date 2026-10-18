#!/usr/bin/env bash
# SessionStart index. Reindexes the project's knowledge base so a session always reads what is on
# disk. Incremental, seconds when nothing changed. The knowledge base is found by its knowledge-base-schema.json,
# at the project root, under docs/ or under knowledge-base/. One stdout line becomes session context, the full index
# report goes to <knowledge base>/_meta/ingest-auto.log. Never crashes and always exits 0, a hook
# failure must not tear down the session.
set -uo pipefail

project="${CLAUDE_PROJECT_DIR:-$(pwd)}"
plugin_root="$(cd "$(dirname "$0")/.." && pwd)"

knowledge_base=""
for candidate in "$project" "$project/docs" "$project/knowledge-base"; do
    if [ -f "$candidate/knowledge-base-schema.json" ]; then
        knowledge_base="$candidate"
        break
    fi
done
[ -n "$knowledge_base" ] || exit 0

log_dir="$knowledge_base/_meta"
run="$(mktemp)"
trap 'rm -f "$run"' EXIT

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') session-start index ==="
    "$plugin_root/bin/studio" index --knowledge-base "$knowledge_base"
} >"$run" 2>&1
status=$?

mkdir -p "$log_dir" 2>/dev/null && cat "$run" >>"$log_dir/ingest-auto.log" 2>/dev/null

summary="$(grep -E '^Indexed in ' "$run" | tail -1)"
standing="$(grep -E '^Standing in the knowledge base: ' "$run" | tail -1)"
if [ "$status" -eq 0 ]; then
    echo "knowledge-base: knowledge base reindexed, retrieval is current. ${summary}"
elif [ -n "$standing" ] && grep -qE '^Validation: 0 error' "$run"; then
    # The run's own pages are clean, so the exit names errors on unchanged pages and the load committed.
    echo "knowledge-base: knowledge base reindexed, retrieval is current. ${summary}. ${standing}. Full report in _meta/ingest-auto.log."
else
    last="$(grep -v '^[[:space:]]*$' "$run" | tail -1)"
    echo "knowledge-base: index failed, retrieval may be stale (${last}). Full report in _meta/ingest-auto.log."
fi
exit 0
