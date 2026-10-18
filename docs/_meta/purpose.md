# Purpose

The knowledge estate of the engram monorepo, indexed as one knowledge base (`repo-docs`). It answers "why does this repo work the
way it does?" from retrieval instead of archaeology: every governed document, decisions, architecture, reference,
how-to, glossaries, plus the working layer (initiative analyses, designs and plans), the factored memory units with their provenance,
and the code plane (`mod.*` pages linking packages to the decisions that govern them).

## How to move

The map groups pages by owning app (`root`, `core`, `engram`, `studio`, `studio-client`, `client-core`). Every page's
`kind` says what it is: `adr` for decisions, `architecture` and `reference` for the consolidated system, `explanation`
for the why, `spec-doc`/`plan-doc`/`brainstorming-doc` for work in flight, `memory-unit` and `hub` for factored working
knowledge, `module` for the code plane. Resume a thread by opening its brainstorm or hub. Answer "what governs this
package" by opening its `mod.*` page; answer "why X" by searching, then descending from the hit. Search is the escalation,
Search is the escalation, never the only way in.

## Out of scope

Generated split-doc artifacts and source code itself. Code is addressed
through the module pages, never stored here.

## Consumers

Agents retrieving over the engram MCP surface or the in-process retrieval capability. Humans reading the same files as
the knowledge base folder opened in Obsidian, or on disk. The knowledge base indexes again from the markdown sources. The database is always the derived copy.
