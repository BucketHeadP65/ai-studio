---
id: adr-0018-corpus-bundles-and-provider-packages
type: adr
title: A corpus's vocabulary lives in its bundle, a provider owns its host
summary: 'Corpus vocabulary leaves engram, studio, harness and the SPA for a top-level bundles/ package, provider implementations
  leave the compiler for a top-level providers/ project, and one spec-declared decision primitive replaces the flag surfaces'
status: proposed
aliases:
- adr-0018-corpus-bundles-and-provider-packages
app: root
absorbs:
- '[[spec-2026-09-01-corpus-bundles]]'
created: '2026-09-02'
---

## Context

engram is a domain-general corpus compiler. A corpus declares its kinds, folders, fields, link fields and per-kind status
vocabularies in `corpusspec.json`, and the compiler, the retrieval doors and the applications over them are meant to work from that
grammar alone. One corpus family's vocabulary, the research ontology, was hardcoded above the grammar in five layers at once.

The kind `flag`, its folder, its fields, its four statuses and its three verbs lived in an engram retrieval module, in an authoring
door, in a studio CLI group, in an HTTP route, in the plugin's session-start hook and in the dashboard's own copy of the status
enum. The repo's own docs corpus declares no flag kind, so every one of those surfaces was inert for it, and the first vocabulary
change to the ontology (a fourth status) missed the dashboard copy. The research spec was the fallback wherever a spec was missing,
so a corpus of another shape that forgot its `corpusspec.json` compiled as research instead of being told to declare itself. The
collection name `research` was the default in the api layer, in evals, in the DB bootstrap and in the eval advisor. The research
ingestion workflows shipped inside the agent runtime beside generic ones, so the runtime carried one corpus's ingestion logic as if
it were the engine's. The authoring doors read and wrote three source field names as SQL literals and folded one vendor's item
types through a fixed set. The runtime defaulted its mounted corpus's name to `research` and described it in research prose. And
each medium class was bound to one host inside the compiler, with the three provider implementations and their fetcher
dependencies shipping in the compiler package, so adding a provider meant editing engram core and compiling any corpus meant
installing fetchers it never called.

The cost was the leak itself rather than any single bug. Every corpus-specific literal in a domain-general layer is a copy of the
spec that drifts, a surface that means nothing for another corpus, and a barrier to anyone defining their own corpus and
workflows.

The standing decisions this sits on. Root [ADR 0007](0007-consolidate-shared-python-into-core.md) fixes the ownership rule
(general-purpose code in the shared libraries, an app's domain in the app) and this decision adds a third clause to it. engram
ADR-0014 made the corpus spec domain-general but left the research ontology as the bundled default, which is the leak one layer
down. engram ADR-0024 named the research profile's privilege at three seams as an honest, bounded acceptance, and this decision
retires that acceptance rather than renewing it. Root [ADR 0016](0016-studio-corpus-registry-and-write-gates.md) makes a corpus a
registry row plus a folder plus a spec, which is what a bundle initializes.

The constraints that ruled the alternatives out. The monorepo owns every caller, so a replaced surface is deleted in the change
that lands its replacement rather than aliased. Workflow names and titles are the owner's and never change when a file moves,
because live schedules follow a workflow by name. Vault paths and vault names stay off agent-facing surfaces (DD-18). And a
corpus's spec, its guidance pages and its workflows are one thing the owner maintains, not three things in three packages.

## Decision

Two rules, and the mechanisms that make them hold.

**A corpus's vocabulary lives in its bundle**, never in engram, studio, harness or the dashboard. **A provider owns its host** and
lives in `providers/`, never in the compiler.

D1. One primitive, decide a page. `decide_page(corpus, page_id, status, links, reason)` moves one page of any kind to a status in
its kind's declared `status_values`, writes each named link field as links, and records the reason on the page. It is the only way
any surface changes a page's status. The shape of a decision is grammar level (a status plus links), the kind is the corpus's
business.

D2. How a kind is decided is declared on the kind. A page type may carry a `decision` block naming its open status and its
outcomes, each outcome optionally requiring one link field. The spec validator checks every status against `status_values` and
every link against `edge_fields`. A corpus's pending decisions are every page of a decision kind sitting in its open status.

D3. Surfaces replace, they do not add. One route lists the pending decisions with each kind's outcomes and one route decides a
page and enqueues the corpus's ingest. One CLI verb lists and one decides. One authoring door decides. The plugin hook announces a
count through the CLI without naming a folder or a kind, and the dashboard card renders rows and outcomes from the API with status
and kind as opaque strings. Every flag-named surface was deleted in the same change.

D4. No default spec. A corpus without `corpusspec.json` is refused with a pointer to `studio init --bundle` and the known bundles.
The validator, the compile handlers, the authoring doors and the runtime's corpus adapters take a spec or fail. Test suites take
their fixture specs from `engram.testing`, which reads the research bundle by path for parity cases.

D5. Bundles are a top-level package. `bundles/<slug>/` holds `corpusspec.json`, the `_meta/` guidance pages, `workflows/steps/`
and `workflows/workflows/`, optional evals, and a README. Ours are `research` (two vaults initialize from it) and `repo-docs`
(this repo's own docs corpus). The mechanism guidance pages ship as proven defaults, the mission pages ship as skeletons the
`engram-init` interview fills. A generated schema page and the vault-local dashboard and log pages are never templated. Chosen
over shipping the default inside engram (the compiler must stay spec-agnostic), over shipping it in the runtime (which keeps only
what is generic over the grammar), and over one repository per bundle (premature with one maintainer, DD-19).

D6. Generic workflows stay in the runtime, custom ones move to their bundle. A workflow is generic when every literal it uses
reaches it through the spec. The research digest and compound passes moved to the research bundle, the two docs passes to the
repo-docs bundle, and the source-removal workflow stayed generic once its leftover write started targeting the spec's decision
kind. A step used by workflows in more than one bundle stays in the runtime's own block directory, so a bundle may depend on a
runtime-shipped generic step. `harness pack <dir>` already takes any directory holding `steps/` and `workflows/`, so packing a
bundle is a path, and the evals runner takes an artifacts directory so a bundle's eval datasets are reachable from where they
live.

D7. A vault owns its copy. `studio init --bundle <slug>` copies the bundle's spec and guidance into the vault, which may then
drift. The bundle is the template and the workflows' home, not a live dependency (DD-20, DD-21).

D8. Enforcement is structural, plus the rule. No package outside `bundles/` imports or reads a bundle, with two named exceptions
(the bundle resolver behind `studio init --bundle` and the test fixture module), held by an import-boundary test in each Python
project. The routes and the CLI take kinds, statuses and fields only from the spec or the request, so a literal has no slot to live
in. The rule itself is a clause of the root ownership rule, and a new literal is a review failure. A source scan for forbidden
words was rejected: it misses leaks not spelled as literals, fires on ordinary English, and needs a growing exclusion list.

D9. The default collection is the session's, never a name. The search function's collection default is the session setting, and
the evals commands, the DB bootstrap and the advisor take the collection as a required argument or from settings.

D10. The sources layer names its fields through the spec. The spec's `sources` block declares the mirror key field, the digest
link field and the source hash field, and the doors build their SQL and their skeletons from those names. One vendor's item-type
folding moved behind the provider that knows it. The four medium classes (video, paper, post, article) stay in engram as the
sources grammar: they name the medium a provider hands over, never the provider, and each spec maps them to its own page kinds.

D11. The runtime names its corpus by its role. The chat corpus door is `corpus`, the mounted corpus's name has no default and the
boot refuses without one, the domain text is read from the corpus's own `api.about`, and no code path compares a corpus name to a
literal.

D12. Corpus templates are bundles. The scaffold preset written in Python and the template written in TypeScript are both gone,
replaced by `studio init --bundle <slug>` and a bundles list the create dialog reads from the API.

D13. A provider owns its host, engram owns the ports. engram keeps the provider and converter ports, the resolver, the mirror, the
sync, the intake ledger and the feeds. A provider recognizes its own URLs, derives their stable keys, fetches, converts, fills in
whatever deep link it owns, and declares the medium class of what it yields. No provider is the default, the registry is the one
place a provider is named, and the mirror model's fields are `source_key` and `source_link`.

D14. Providers leave the compiler. The top-level `providers/` project (package `engram_providers`, distribution
`engram-providers`) holds one subpackage per provider behind its own optional extra, registered through two entry-point groups
that engram resolves lazily by name. engram compiles with none installed and its own test suite never needs one, because the
registry takes an injectable source. Both applications depend on the distribution with the extras they use, because the doors that
classify a pasted URL run in both processes. Chosen over optional extras inside engram: the compiler package stays spec-agnostic
and dependency-light, and a third party adds a provider by publishing one, never by editing engram.

## Consequences

Easier. A corpus of any shape gets the whole machinery from its own spec: its decision kinds appear in the CLI, the API, the hook
and the dashboard with its own words, and the repo's docs corpus stops carrying five layers of inert flag plumbing. The owner
maintains the research spec, its guidance pages and its two workflows as one bundle instead of as fragments in three packages, and
a new vault starts from a bundle in one command. A third party installs the three projects, takes a bundle or writes their own
spec and workflows, and finds no research vocabulary in the tools they run. Adding a provider is publishing a distribution that
registers an entry point, and a corpus that reads no sources installs no fetchers. Feed scanners resolve the same way through a
third entry-point group, `engram.sources.feed_scanners`: a watched feed's `kind` is a scanner's registered name, engram ships the
generic `xml` reader and no host scanner, and the X scanner lives in the providers project behind its own extra.

Harder. A corpus with no `corpusspec.json` is now refused rather than compiled, so every fixture and every vault must declare
itself. A spec that enables sources must declare its three source field names and its provider list, so an older spec is refused
until it does. The four medium classes are engram's grammar and the dashboard's enum stays paired with them by hand, since nothing
generates one from the other. Bundles are a directory tree rather than an installable distribution (DD-19), so a consumer outside
this checkout reaches one by path or by the bundles environment variable. Providers resolving by entry point means a stale
editable install shows up as a provider that is not installed rather than as an import error, so a layout change is followed by a
reinstall of the affected package in every consumer.

What the repo must keep doing. The import boundary per project and the ownership clause in the root guidance are the whole
enforcement, so a reviewer refuses a corpus literal in a domain-general layer on the rule, not on a test that cannot see it. A
workflow that moves keeps its name and its title, because live schedules follow a workflow by name. A replaced surface is deleted
in the change that lands its replacement, never aliased. Four residues are known and named rather than silently carried. The
compile tier's join columns for mirror citers keep their vendor-derived names, because renaming them is a migration plus
compile-handler work. The OKF export projector still builds one vendor's deep link from a provider-name comparison, the pattern
the sync path shed. The projector's coupling is not only that comparison: it is the whole OKF projector's research shape,
concentrated in one seam, `_ResearchFields`. The MCP envelope surface keeps a default collection constant, because the
collection should come from the host and removing it changes every caller of the server factory. And the Zotero host's settings
model still lives in `engram.config` and types `SourceResources.library`. The clean shape is a provider-keyed extras mapping
with the model inside `engram_providers.zotero`. All four are recorded as debt rather than left to be rediscovered.

## Options considered

Keep the flag door and make its kind configurable. Lost because it keeps a flag-shaped verb set (accept, dismiss, complete) that
another corpus's decision kind may not share, when the only grammar-level part of a decision is the status and the links.

Derive the decision kinds from the compounding block's owner-gated entries. Lost because the gate says how a page lands, not how
it is decided, and a hand-written page of the same kind would never appear. The two compose instead.

Keep the research spec and its workflows inside engram as the default. Lost because the compiler must stay spec-agnostic, and a
bundled default is the same leak one layer down.

Keep the corpus workflows in the agent runtime beside the generic ones. Lost because the runtime would ship one corpus's ingestion
logic as the engine's, which is the thing this decision removes everywhere else.

One repository per bundle. Right once a bundle has a consumer outside this repo, premature with one maintainer. Deferred as DD-19,
together with publishing a bundle as an installable distribution.

A test that scans source for forbidden vocabulary words. Lost because it misses every leak not spelled as a literal, fires on
ordinary English words the grammar itself uses, and needs an exclusion list that grows forever.

Provider implementations as optional extras inside engram. Lost because the compiler package would keep the provider names and
their dependency graph, and a third party would still have to edit engram to add one.
