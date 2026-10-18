# Note grammar and CLI validation

Read `./knowledge-base-schema.json` first, it is the knowledge base's schema, naming the page types, relations, entity types,
claim types, facets, and whether claims and sources are enabled. For the page type you are authoring, run
`"${STUDIO_BIN:-studio}" note schema --type <page_type> --knowledge-base .` for its exact JSON Schema, the required and optional
fields, facets, section headings, and claim grammar. The schema output is authoritative, do not infer fields
from examples.

## The payload

An `AuthoringPayload` is a JSON object with three top level keys.

```jsonc
{
  "frontmatter": {
    "id": "<id_prefix>.<slug>",
    "type": "<page_type>"
    // required and applicable optional frontmatter fields
    // link fields render as "[[target-slug]]", link lists as ["[[a]]", "[[b]]"]
  },
  "sections": [
    {"heading": "<H2 heading>", "body": "<section prose, H3s allowed>"}
    // never inline claims in a section body, they render from the claims array below
  ],
  "claims": [
    // only when claims are enabled for this type
    {
      "text": "<atomic, self contained claim sentence>",
      "kind": "<registered claim type>"
      // plus the fields the schema's claims block declares, such as a locator, a verbatim quote,
      // a subject link "[[page-slug]]" and edges as [["<relation>", "[[target]]"]]
    }
  ]
}
```

Prose you write before the first H2, the introduction under the page title, is indexed as the page's own
preamble unit: it is searchable and fetchable like any section, so an opening paragraph is worth writing
rather than avoiding.

Wikilinks are bare slugs, `[[page-slug]]`, never the prefixed page id. Use only registered vocabulary
(facet values, entity types, claim types, relation types) from the knowledge base's `_meta/vocabulary/` files and
`knowledge-base-schema.json`. A value missing from the vocabulary is a proposal, never an invention.

## Citing pages and claims in section prose

A page is cited as `[[its-file-stem|its.full.id]]`, the file stem as the wikilink target (what the knowledge base
folder, opened in Obsidian, resolves) with the full page id kept visible as the display text. A claim is cited as
`[[its-file-stem#^cN|^cN]]`. Never leave a page id or a claim anchor bare in prose, and never cite an anchor without
the page that defines it, a bare `^c4` names nothing once the sentence leaves its page. The write tool rewrites a
bare id or anchor into the proper link when it provably resolves and reports every rewrite, or every citation it
could not resolve, in its warnings. Fix what it names before finishing.

## Build and validate

Write the payload to a scratch file, then render it.

```bash
"${STUDIO_BIN:-studio}" note build --payload /tmp/<slug>.json --out <knowledge-base-relative-path>.md --knowledge-base .
```

Add `--verify-quotes` only when the knowledge base has `sources.enabled: true` and `quote_verify: true`. A non-zero exit
means nothing was written, read the error, fix the payload, and rebuild. Never hand edit the rendered file to
silence a build failure. Add `--force` to overwrite an existing note during iteration.

```bash
"${STUDIO_BIN:-studio}" note validate <knowledge-base-relative-path>.md --knowledge-base .
"${STUDIO_BIN:-studio}" note verify <knowledge-base-relative-path>.md --knowledge-base .
```

`validate` reruns the parse and vocabulary checks, fix any grammar or vocabulary error before continuing (a
`dangling-link` warning is expected noise mid session, only errors and verify failures gate). `verify` applies only
when the knowledge base has sources enabled with quote verification, and reruns the quote in mirror check, iterate
the quotes in the payload and rebuild with `--force` until it exits clean.

## Index

Once the note is validated (and verified where that applies), index it.

```bash
"${STUDIO_BIN:-studio}" index --knowledge-base .
```

Indexing is a separate step, author several notes and index once at the end.
