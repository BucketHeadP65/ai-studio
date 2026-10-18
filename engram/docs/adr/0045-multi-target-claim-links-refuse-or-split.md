---
id: engram-adr-0045-multi-target-claim-links-refuse-or-split
aliases: ["engram-adr-0045-multi-target-claim-links-refuse-or-split"]
type: adr
app: engram
status: accepted
tags: []
title: "0045 — A claim field with two link tokens splits if it is an edge, refuses if it is a scalar"
---

# 0045 — A claim field with two link tokens splits if it is an edge, refuses if it is a scalar

**Status:** Accepted (2026-08-23)

**Context.** `UnitLink._UNIT_LINK_RE` anchored a `[[…]]` value at both ends (`^\[\[(?P<body>.+?)\]\]$`) but placed no
restriction on what the body itself could contain. A claim field carrying two link tokens back to back —
`metric:: [[f1-score]], [[exact-match]]`, both well-formed `[[…]]` links, both target pages present — still matched
the anchored pattern once as a whole: the non-greedy body expanded past the first `]]` to satisfy the trailing `$`,
capturing `f1-score]], [[exact-match` as one corrupted target. The claim parser's `link_scalar_fields` and
`edge_relations` branches each called `UnitLink.parse` on the raw field value directly, so this was silent: the
field parsed to *something*, just the wrong thing. The live symptom was a standing lint row —
`unresolved metric link 'f1-score]], [[exact-match'` — pointing at a real note whose author had written two
perfectly valid links on one `metric::` line, one target after another.

`multilink_fields` (`on_dataset`, `disputed_by`) already split on link tokens before parsing each one
(`_split_links`, via `_UNIT_LINK_TOKEN_RE`), so they never hit this — the bug lived specifically in the two branches
that parsed a field's value as a single link. That split between "fields the grammar already treats as
many-valued" and "fields the grammar treats as one-valued" is the fork this decision resolves: once the underlying
regex is fixed so a value can no longer parse as one link across a `]], [[` boundary, a two-token value on a
one-valued field has nowhere to go — it must either become an error, or the field's arity must change.

**Options considered.**

- **Refuse everywhere** (both edge relations and link-scalar fields raise on more than one token). Rejected: an edge
  relation is *conceptually* many-valued already — `supports`/`refutes`/`replicates`/`outperforms`/`comparable_to`
  are `ClaimBlock.edges: tuple[tuple[str, UnitLink], ...]` in the domain model, a list of pairs, with no arity limit
  the model itself enforces. Refusing a second `supports::` target on one line would force an author to repeat the
  whole `key:: [[target]]` bullet per target for a relation the model already stores as a list, for no grammar
  benefit.
- **Split everywhere** (a link-scalar field with two tokens takes the first, or concatenates into a list). Rejected:
  `ClaimBlock.subject`, `.on_task`, and `.metric` are typed `UnitLink | None` in the domain model — genuinely
  one-valued, not a list the model happens to render as one line. Silently keeping the first token (or the last)
  reintroduces exactly the failure mode this fix closes: a value the author wrote with two targets in it produces a
  result that looks like it parsed correctly while dropping one target, with no signal that anything was lost.
- **Split edge relations, refuse link-scalar fields** (chosen). Each field's grammar cardinality now matches its
  behavior on a multi-token value: an edge relation's `tuple[...]` shape accepts as many targets as are written, one
  `ClaimBlock.edges` entry per token, in the order written; a link-scalar field's `UnitLink | None` shape cannot
  represent more than one target, so it raises `ClaimParseError` naming the field and the token count rather than
  picking or merging.

**Decision.**

1. `_UNIT_LINK_RE` (`domain.py`) gains a negative-lookahead body (`(?:(?!\]\]).)+?`) so no `[[…]]` match can ever
   cross a `]]` boundary — `UnitLink.parse("[[a]], [[b]]")` now raises `NoteGrammarError` instead of returning one
   link with a corrupted target. The four grammar forms (`[[target]]`, `[[target|alias]]`, `[[target#heading]]`,
   `[[target#^block]]`, and their combinations) are unaffected — none of their bodies ever contain `]]`.
2. `parser._absorb_field`'s `edge_relations` branch now reuses `_split_links` (the same helper `multilink_fields`
   already used) and appends one `(relation, UnitLink)` edge per token found, in written order. A single-target line
   behaves exactly as before; `supports:: [[a]], [[b]]` now yields two `supports` edges instead of one corrupted one.
3. The `link_scalar_fields` branch counts the tokens `_split_links` finds in the value; more than one raises
   `ClaimParseError` with a message naming the field and the count (e.g. `"field 'metric' takes one link, got 2"`).
   Exactly one token (or a value that is not link-shaped at all) parses exactly as before, so the error path only
   fires on the newly-detected case.
4. This is caught, not raised further: `compile/handlers.py`'s `_parse_stage` already wraps `parse_note` in a
   broad `except Exception`, turning any `NoteGrammarError` (parent of `ClaimParseError`) into a page-level
   `parse-error` lint row instead of aborting the compile. A note with a bad-arity link-scalar field still loads the
   rest of the corpus; only that one note's parse fails, exactly as a missing `loc::` already did.

**Consequences.**

- (+) **The corruption case is closed.** A link-scalar field can no longer silently parse to a target the author
  never wrote; it either parses to the one target written or fails loudly.
- (+) **Edge relations gained real multi-target authoring** for free: one line, several targets, in the order
  written, matching the arity the domain model already had.
- (−) **A note authored with two targets on a link-scalar field, previously silently mis-parsed, now fails to
  parse** and surfaces as a compile-time lint error instead. This is the live-corpus case (`metric::` on
  `kag-2024.md`) that motivated the fix — the author must either move the second target off the scalar field or
  restructure the claim (e.g. two claims, one per metric) once the lint flags it. No corpus data is silently
  mis-parsed as a result of this decision; existing single-target values are unaffected.
- **Reversible?** Yes. Both the regex hardening and the branch changes are contained to `domain.py` and
  `parser.py`; reverting either restores the exact prior parsing behavior (bug included).

**Related.** Code: `engram/src/engram/memory/domain.py` (`UnitLink`, `_UNIT_LINK_RE`), `engram/src/engram/memory/parser.py`
(`_absorb_field`, `ClaimParseError`), `engram/src/engram/compile/handlers.py` (`_parse_stage`'s fault-tolerant parse),
`engram/docs/reference/knowledge-base.md` (the note-grammar kernel's public contract).
