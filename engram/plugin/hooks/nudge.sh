#!/usr/bin/env bash
# SessionStart nudge. Finds the project's knowledge base by its knowledge-base-schema.json, at the project root,
# under docs/ or under knowledge-base/. Prompts /knowledge-base:init when none exists, else prints the knowledge base and database
# identity lines, then announces pages awaiting a decision.
# Never crashes and always exits 0: a hook failure must not tear down the session.
set -euo pipefail

project="${CLAUDE_PROJECT_DIR:-$(pwd)}"
plugin_root="$(cd "$(dirname "$0")/.." && pwd)"

main() {
    local knowledge_base=""
    for candidate in "$project" "$project/docs" "$project/knowledge-base"; do
        if [ -f "$candidate/knowledge-base-schema.json" ]; then
            knowledge_base="$candidate"
            break
        fi
    done
    if [ -z "$knowledge_base" ]; then
        echo "knowledge-base: this knowledge base is not initialized, use the engram-init skill (Claude Code: /knowledge-base:init) to set up the schema and DB."
        return 0
    fi
    "$plugin_root/bin/studio" status --knowledge-base "$knowledge_base" 2>/dev/null | grep -E '^(knowledge base |db )' | head -2 || true
    local count
    count="$("$plugin_root/bin/studio" decisions --count --knowledge-base "$knowledge_base" 2>/dev/null | tail -1)" || return 0
    case "$count" in
        ''|*[!0-9]*) return 0 ;;
    esac
    if [ "$count" -eq 1 ]; then
        echo "knowledge-base: 1 page awaits a decision in this knowledge base, run 'studio decisions' to read it."
    elif [ "$count" -gt 1 ]; then
        echo "knowledge-base: $count pages await a decision in this knowledge base, run 'studio decisions' to read them."
    fi
}

main || true
exit 0
