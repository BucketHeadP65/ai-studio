---
id: engram-research-03-scholarly-schemas
aliases: ["engram-research-03-scholarly-schemas"]
type: explanation
app: engram
status: current
tags: [research]
title: "03: Scholarly knowledge schemas (research notes)"
---

# 03: Scholarly knowledge schemas (research notes)

*Research notes that grounded engram's [product decisions](../product-decisions.md).*

Purpose: survey how scientific knowledge is formally schematized (ORKG, nanopublications, Papers with Code, Wikidata, scite, SciFact, OpenAlex/S2) to inform the research knowledge base's claim schema, entity-type registry, and relation-type registry. Progressive per-source notes. Owned by the schema-research task.

## ORKG: Open Research Knowledge Graph (model, contributions, comparisons, templates)

- URLs: https://orkg.org, walkthrough paper https://ar5iv.labs.arxiv.org/html/2206.01439, comparisons paper https://arxiv.org/abs/2006.01747 (Oelen et al., JCDL 2020)
- Accessed: 2026-06-10
- Unit of description is the **ResearchContribution**, not the paper: a paper has 1..n contributions. Each contribution links a *research problem*, *method(s)*, and *result(s)* plus arbitrary property/value statements. Comparison happens at contribution granularity ("compare specific elements of a paper instead of the complete paper").
- Graph primitives: statement = (resource) -[predicate]-> (resource | literal). Unlike plain RDF, statements/edges carry annotations (provenance: timestamps, attribution). Properties are first-class resources, so the property vocabulary itself is community-curated and reusable.
- **Comparisons are first-class published objects**: table with contributions as columns and properties as rows. Built by (a) finding similar contributions, (b) aligning properties, exact ID match + fastText embedding similarity (threshold τ≥0.9) + manual user correction, (c) visualizing, (d) publishing.
- Published comparison carries: persistent ID, Dublin Core metadata (title, description, creation date, creator), versioned/immutable snapshot, exports (CSV/RDF/PDF/LaTeX), data modeled with RDF Data Cube. DOIs for comparisons (planned then, since shipped). Display heuristic: show properties shared by ≥2 contributions (α≥2).
- Empirics: ~10 properties per contribution on average. 169 papers gave 3,750 statements in their survey reconstruction.
- **Templates** = SHACL-style shapes (UI has "Import SHACL"): a domain expert pins the set of predicates + expected classes/datatypes/cardinality for contributions about a given research problem/field, this is what makes cross-paper comparison columns line up. E.g. leaderboard template ≈ Task-Dataset-Metric-Value.
- Lessons for the knowledge base: (1) make "comparison" a first-class, citable document built from per-paper structured claims, (2) property alignment is the hard part, a shared template/registry beats post-hoc embedding alignment, (3) contribution-level (not paper-level) granularity, (4) version + snapshot comparisons.

## Nanopublications: assertion / provenance / pubinfo triad

- URLs: https://nanopub.net/, guidelines https://nanopub.net/guidelines/working_draft/, overview paper https://arxiv.org/pdf/1809.06532
- Accessed: 2026-06-10
- A nanopub = 4 named RDF graphs: **head** (glue: `np:hasAssertion`, `np:hasProvenance`, `np:hasPublicationInfo`), **assertion** (the atomic claim as triples), **provenance** (how the *assertion* came about: `prov:wasDerivedFrom` source/study, `prov:wasAttributedTo` who asserted, methods/parameters of the study), **pubinfo** (metadata of the *nanopub itself*: `dct:created`, `dct:creator`, license).
- Key separation to steal: provenance-of-the-claim vs metadata-of-the-record are distinct layers. "Who measured X" ≠ "who wrote this note".
- Citability: each nanopub gets a **Trusty URI**, content-addressable identifier embedding a cryptographic hash, making it verifiable, immutable, permanent, signed with the author's key, published to a decentralized server network. The claim itself becomes the citable unit, independent of the paper.
- Atomicity is pragmatic, not dogmatic: guidelines allow one statement or a small group of inseparable statements per assertion (example: trastuzumab-breast-cancer indication).
- Ecosystem: nanopub templates (e.g., via Nanodash) constrain assertion shape per claim type, same role as ORKG templates.
- Markdown translation: a claim should carry (a) the claim text/triple, (b) provenance fields (source paper, section/quote anchor, method that produced the evidence), (c) record metadata (who/when/tool extracted it, license), and a stable claim ID (hash of content ≈ poor-man's trusty URI).

## Papers with Code: task/dataset/method/metric/result model + 2025 sunset

- URLs: data dumps https://github.com/paperswithcode/paperswithcode-data, schema https://github.com/paperswithcode/sota-extractor, client model https://github.com/paperswithcode/paperswithcode-client (models/evaluation/result.py), shutdown thread https://github.com/paperswithcode/paperswithcode-data/issues/116
- Accessed: 2026-06-10
- Core ontology: **Task** (with subtasks) to **Dataset** (per task) to **Sota** (leaderboard: list of metric names + rows) to **SotaRow**. Separately: **Methods** (techniques, organized in collections/categories) and paper↔code links. An evaluation table binds (task, dataset). A row is "model M achieves {metric: value} on dataset D for task T", i.e., a claim with built-in qualifiers.
- sota-extractor SotaRow fields: `model_name`, `paper_title`, `paper_url`, `paper_date`, `code_links[]`, `model_links[]`, `metrics{name→value}`.
- API client Result fields (richer): `id`, `best_rank`, `metrics{}`, `methodology`, `uses_additional_data` (bool, a *condition qualifier*), `paper`, `best_metric`, `evaluated_on` (YYYY-MM-DD), `external_source_url`.
- **Sunset**: Meta shut paperswithcode.com down ~July 24-25, 2025. Domain now redirects to Hugging Face Papers ("Trending Papers", launched with Meta partnership). At death: ~9,327 benchmark leaderboards, ~79,817 paper-code links, ~5,628 datasets. HF successor has papers + some leaderboards but **no comprehensive task-based SOTA tracking**. Final data dumps frozen on HF/GitHub (CC-BY-SA), no longer updated. Niche successors (e.g., CodeSOTA) are partial.
- Lessons: (1) `uses_additional_data` + `evaluated_on` + `methodology` are exactly the qualifier set a benchmark claim needs, (2) metrics as open dict (registry of metric names) rather than fixed columns, (3) leaderboard = materialized view over claims sharing (task, dataset), an assembled comparison, (4) centralized aggregator died, argues for claims living in durable plain-text sources assembled into views.

## Wikidata: statement model: property + value + qualifiers + references + rank

- URLs: https://www.wikidata.org/wiki/Help:Statements, https://www.wikidata.org/wiki/Help:Qualifiers
- Accessed: 2026-06-10
- Model layers: **claim** = (subject item, property, value) + **qualifiers** (property+value pairs refining the claim). **Statement** = claim + **references** (sources, "Wikidata says what the population is *according to* a source", not what is true) + **rank** (preferred / normal / deprecated) for conflicting values.
- Qualifiers refine *meaning*. References support *belief*. Rank arbitrates *conflict*. Three distinct mechanisms, don't conflate them in the knowledge base schema.
- Qualifier page_types most relevant to science claims (verified P-ids): **P585** point in time / **P580-P582** start-end time (temporal validity), **P459** determination method (how measured), **P1013** criterion used, **P518** applies to part / **P642** "of" / **P1011** excluding (scope restriction), **P1480** sourcing circumstances (circa, allegedly, disputed), **P5102** nature of statement (hypothesis, de facto, epistemic status), **P2210** relative to (comparisons).
- Properties are typed (datatype restricts value: item, quantity, time, URL...). Quantities carry units and uncertainty bounds natively.
- Multiple values for one property coexist, each contextualized by qualifiers (population per year), same pattern as one method having many benchmark results.
- Lesson for the knowledge base: a claim = core triple + open `qualifiers:` map (dataset, condition, metric, method, as-of date, scope) + `evidence:`/`source:` list + an explicit `status`/rank field for superseded or disputed claims.

## scite.ai: Smart Citations: claim-level citation classification

- URLs: QSS paper (Nicholson et al. 2021, doi:10.1162/qss_a_00146) https://direct.mit.edu/qss/article/2/3/882/102990, mirror https://ouci.dntb.gov.ua/en/works/4NmQAgV7/ (MIT/bioRxiv/help-center fetches 403'd, details below from paper abstract + secondary reports)
- Accessed: 2026-06-10
- Unit = **citation statement**: the in-text citation plus its surrounding textual context from the citing paper's full text. Each Smart Citation displays the snippet so readers can judge the claim relationship themselves.
- Taxonomy (3 classes, about the cited work's findings): **supporting** (citing paper provides evidence supporting the cited work), **contrasting** (evidence contradicting/disputing it), **mentioning** (neither, mere reference). Deliberately coarse. Intent ≠ sentiment.
- Scale/skew: trained on ~40k manually annotated citation statements (deep learning), over a collection of >25M full-text articles, giving >880M classified statements. Distribution ~92.6% mentioning / 6.5% supporting / 0.8% contrasting, *evidential* citations are rare. An external audit found mentioning is over-assigned (recall on supporting/contrasting is the weak spot).
- Also surfaces editorial context per paper: retraction / correction / erratum notices alongside citation tallies.
- Lessons: (1) classify *edges between claims/papers*, not papers, our relation registry needs `supports` / `contradicts` / `mentions(neutral cite)` with the citing snippet stored as evidence, (2) keep the quote, classification without the underlying snippet is unauditable, (3) expect extreme class imbalance: most citations are non-evidential, so don't force editors to pick support/contradict.

## SciFact & successors: claim/evidence for verification

- URLs: SciFact paper https://ar5iv.labs.arxiv.org/html/2004.14974 (Wadden et al., EMNLP 2020), repo https://github.com/allenai/scifact, SciFact-Open https://aclanthology.org/2022.findings-emnlp.347/, MultiVerS https://arxiv.org/pdf/2112.01640
- Accessed: 2026-06-10
- Claim definition (the canonical one): "an **atomic verifiable statement** expressing a finding about **one aspect of one scientific entity or process**, verifiable from a single source." Atomic = single property of a single entity. Complex claims get decomposed.
- Schema: claim (one sentence, NL) to per-abstract evidence: {label ∈ **SUPPORTS / REFUTES / NOINFO**, rationale sentences (minimal sentence set justifying the label)}. 1,409 claims / 5,183 abstracts. κ=0.75 on labels. Verification pipeline = abstract retrieval to rationale selection to label prediction.
- Claims were rewritten from **citation sentences** (citances). REFUTES examples authored by negating claims via effect-direction reversal, not surface negation.
- **SciFact-Open** (500K-abstract open collection) surfaced the key real-world wrinkle: evidence often **supports only a special case of the claim** (partial/qualified support), exactly why a 3-way label is too coarse without qualifiers/scope on the claim.
- MultiVerS: multitask label+rationale over full documents with weak supervision, labels remain the same triad.
- Lessons: (1) minimal viable claim = 1 NL sentence + entity anchor(s), minimal viable evidence = (source doc, rationale quote(s), label), (3-way label set is the floor), (2) store rationale quotes, not just labels, (3) add a `scope`/qualifier escape hatch for "supports under condition C", the datasets' known failure mode.

## OpenAlex topics & Semantic Scholar SPECTER2/S2ORC (brief)

- URLs: https://docs.openalex.org/api-entities/topics/topic-object, https://help.openalex.org/hc/en-us/articles/24736129405719-Topics, classifier https://github.com/ourresearch/openalex-topic-classification, https://huggingface.co/allenai/specter2
- Accessed: 2026-06-10
- OpenAlex taxonomy (built with CWTS Leiden): 4 levels, **4 domains > 26 fields > 252 subfields > 4,516 topics** (Feb 2024 release). Works get model-assigned `topics[]` with scores. Top one is `primary_topic`. Classifier = fine-tuned mBERT over title+abstract+citations. Topic labels/descriptions LLM-generated from citation clusters. Mapped to Scopus ASJC. Replaces deprecated "concepts". Separate lightweight `keywords` also exist.
- Reuse: adopt topic IDs (or just domain/field/subfield strings) as controlled paper-facet values in frontmatter instead of inventing a taxonomy. Resolvable via OpenAlex API by DOI.
- SPECTER2 (allenai): bert-base + task adapters (proximity/adhoc-query/classification/regression). Embeds **title + [SEP] + abstract** to 768-d. Trained on 6M+ citation triplets, adapters on SciRepEval. Proximity adapter is the default for paper-similarity / related-paper retrieval. S2ORC = the underlying 81M+ full-text collection (also the training substrate for SciFact-style work).
- Reuse: store one SPECTER2-proximity embedding per paper in Postgres (pgvector) for "related papers/claims", paper-level facets (topics) + embedding are complementary, cheap, and standard.

## Cross-cutting synthesis (for schema design)

- Convergent pattern across all seven systems: **claim/statement = core proposition + qualifiers (scope/conditions) + provenance (source + quote anchor) + record metadata + status/rank**, Wikidata names the layers, nanopubs separate them into graphs, PwC hard-codes one domain instance (task/dataset/metric/value + uses_additional_data), ORKG templates them, SciFact gives the NL-atomicity test, scite gives the evidential edge taxonomy.
- Granularity ladder used everywhere: paper to contribution (ORKG) to claim (nanopub/SciFact) to qualified value (Wikidata/PwC). The knowledge base should support paper-level facets AND claim-level blocks.
- Comparison/leaderboard = derived view over claims sharing qualifier keys (ORKG comparisons, PwC leaderboards), index it, version it, make it citable. Don't hand-author it.
- Property/template registries (ORKG templates ≈ SHACL, nanopub templates, PwC metric names, Wikidata properties) are what make claims comparable. Free-form keys are what break comparisons.
