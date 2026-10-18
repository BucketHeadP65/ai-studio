---
name: documenting
description: Use when writing or updating a document in an engram knowledge base (a design, a plan, an analysis,
  any governed page), when starting a piece of work that the knowledge base documents, or when unsure what
  documents this knowledge base expects. The knowledge base itself declares its ceremony, this skill teaches how to
  ask it.
---

# Documenting in an engram knowledge base

The knowledge base owns its documentation policy. Never guess its ceremony, ask it.

1. **Profile first.** Call `knowledge_base_profile(knowledge_base)`. The answer's `page_types` lists every page type with its guidance,
   when and where a page of that type is written, and each section's guidance. The `purpose` text says what the
   knowledge base is for.
2. **Fetch a skeleton.** Call `skeleton(knowledge_base, page_type)` for the type you need. The answer is a ready template:
   frontmatter with the required fields and their valid choices, every canonical section seeded with its guidance,
   and rules naming what to complete. An unknown type is refused with the valid types named.
3. **Write the sections, then write once.** Replace each section's empty body with real content, following that
   section's guidance. Complete the frontmatter (the rules say how). Then call `write_note(knowledge_base, payload)` with
   the finished payload. A refusal names every finding, fix them all and write again, complete. Never write a
   placeholder to see what the note grammar accepts.
4. **Graduate by status.** A document's lifecycle is its `status` field. To move a document forward, write it again
   with the new status. Files never move between folders.

When these tools are not mounted in this session, say so and fall back to the knowledge base's charter (its README
or purpose page) rather than inventing a ceremony. Without them mounted, `grammar.md` beside this file carries the
note grammar and the CLI validation path for writing a note by hand.
