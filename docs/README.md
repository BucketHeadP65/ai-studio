---
id: docs-charter
aliases: ["docs-charter"]
type: explanation
app: root
status: current
tags: [docs-as-code]
title: "Docs charter: how documentation works in this repo"
---

# Docs charter: how documentation works in this repo

Documentation in this repository is docs as code. It lives next to the code it describes, carries frontmatter a
script checks, and is validated like code ([ADR 0005](adr/0005-docs-as-code-standard.md)). Doc types follow
**Diátaxis**, the architecture follows a light **arc42** outline with **C4** diagrams in Mermaid, and decisions are
**MADR**-style ADRs.

**Start at [`CONTEXT-MAP.md`](../CONTEXT-MAP.md)** at the repository root. It indexes every governed doc.

## Where each doc type lives

| `type` | Answers | Lives in |
| --- | --- | --- |
| `architecture` | How is the system shaped? | [`docs/architecture/`](architecture/) for the whole system, `<app>/docs/architecture/` for an app that outgrew its section |
| `explanation` | Why is it this way? | [`docs/explanation/`](explanation/) across apps, `<app>/docs/explanation/` for one app |
| `reference` | What is this slice's contract? | `<app>/docs/reference/<slice>.md`, one per feature slice |
| `how-to` | How do I do X? | `<app>/docs/how-to/` |
| `adr` | What did we decide and why? | [`docs/adr/`](adr/) across apps, `<app>/docs/adr/` for one app |
| `glossary` | What does this word mean? | `CONTEXT.md` in each app, indexed by `CONTEXT-MAP.md` |

[`docs/images/`](images/) holds the screenshots the READMEs show.

## Frontmatter

Every governed doc starts with:

```yaml
---
id: stable-kebab-slug   # never changes, even when the file moves
type: adr | architecture | reference | explanation | how-to | glossary
app: root | core | engram | studio | studio-client | harness
status: draft | current | superseded     # ADRs use proposed | accepted | deprecated | superseded
tags: []
title: "..."
---
```

[`docs/knowledge-base-schema.json`](knowledge-base-schema.json) is the machine-readable contract behind this charter:
the 13 page types, each one's required frontmatter, its status values and the relations between pages.
`scripts/docs/validate_docs.py` checks the docs against it, and `studio index` indexes the same docs into the
`repo-docs` knowledge base. Changing a page type means changing the schema.

## Reference docs

Each feature slice has one reference doc of about 100 to 200 lines under its app's `docs/reference/`. It covers the
purpose, the entry points, the public contract, the runtime flow as a Mermaid diagram, examples, the patterns used and
the seams for tests. It names models and files by path and does not paste code. Function-level detail belongs in the
docstrings.

## Architecture masters and split docs

The system architecture is one master file, [`docs/architecture/architecture.md`](architecture/architecture.md). Every
H2 opens with a `> **TL;DR:**` line and a `<!-- load-when: ... -->` comment of keywords. A script splits the master
into `sections/`, one file per H2, and writes `index.md`, the map a reader or an agent loads first to pick the
sections it needs.

- Edit the master only. `sections/` and `index.md` are generated, so regenerate them and commit them together:

  ```bash
  python3 scripts/docs/split_doc.py --dir docs/architecture
  ```

- An H2 without its `TL;DR` line or its load-when comment makes the script exit 1.
- When an app's H2 in the root master grows past about 150 lines, it moves to `<app>/docs/architecture/architecture.md`,
  split by the same script with `--dir <app>/docs/architecture`, and the root section shrinks to a summary and a link.

## Working documents

The documents that drive a piece of work (an analysis, a design, an architecture and a plan) live one folder per
piece of work under `docs/initiatives/<YYYY-MM-DD>-<slug>/`. They carry governed frontmatter and the
`repo-docs` knowledge base indexes them, but `.gitignore` keeps them out of the repository, apart from the few that
code reads. When the work lands, what should last moves into governed docs: an ADR for the decision, updated
architecture or reference docs, and a `CONTEXT-MAP.md` row. The canonical sections of each document kind are in the
schema, and the guidance for writing them is in [`_meta/documentation-policy.md`](_meta/documentation-policy.md) and
[`_meta/compounding-policy.md`](_meta/compounding-policy.md). The `documenting` skill and the
`knowledge_base_profile`, `skeleton` and `write_note` tools serve the same contract to agents.

## Validation

```bash
python3 scripts/docs/validate_docs.py                     # frontmatter, unique ids, CONTEXT-MAP links, the vocabulary
python3 scripts/docs/split_doc.py --dir docs/architecture # the master's discipline
```

Both use the Python standard library only. Run them before committing a doc change. The validator also reads the
READMEs, the `CLAUDE.md` files, the skills, the template pages, the workflow files and the Studio's UI text, and fails on
a retired word or a banned symbol. Both lists are in `scripts/docs/validate_docs.py`. A new governed doc gets its
`CONTEXT-MAP.md` row in the same change.
