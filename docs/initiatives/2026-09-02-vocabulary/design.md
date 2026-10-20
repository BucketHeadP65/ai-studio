---
id: spec-2026-09-02-vocabulary
aliases: ["vocabulary", "spec-2026-09-02-vocabulary"]
type: spec-doc
title: "One vocabulary: bounded contexts, their words, their events, and the renames that get us there"
status: approved
owner: maintainer
date: 2026-09-02
---

# One vocabulary: bounded contexts, their words, their events, and the renames that get us there

Status: approved by the owner on 2026-09-03, every ruling in section 9 and every R18 swap included. The inventory it works from
counted 325 terms in use, 40 words with more than one meaning, 11 things with more than one word.

## 1. Summary

The repository has four glossaries, one per package, and no shared vocabulary. The same word means different things in
different packages, and the same thing has different words. This document draws the target: six bounded contexts and one
shared kernel, one word per thing and one thing per word, events named as facts in the past tense, commands named as
requests in the imperative, and a rename map from the words in use today to the words that survive. Every word is one
most people already know. The owner has ruled on every contested word (section 9).

## 2. Method

Four general rules and three from the owner decide every row below.

1. **One definition per term.** A term is defined once, in plain words, and every other use points at that definition.
2. **One word, one meaning.** A word carries one meaning across the whole repository. Where a word must serve two
   things, it is always qualified, never bare: registry of steps, registry of skills.
3. **Events are facts, commands are requests.** An event is named in the past tense, appended once, and never refused.
   A command is named in the imperative, has one handler, and may be refused. Three tests decide which one you have:
   tense, audience (exactly one handler acts, or zero to many react), and deletion (remove a listener, and the producer
   must not change).
4. **A context is entered only through its contract.** Its contract module, its HTTP surface, or its boundary events.
   Never its tables, its files, or its settings.
5. **The stranger test.** Every term gets one sentence that uses it. A stranger who reads the sentence must understand
   it at once. A term that fails the test is renamed, not explained.
6. **Speak like a human.** The word a person says is the word in the code. No surface word with a different identifier
   underneath. No leftovers, no half jobs.
7. **Standard names first.** Where most people already agree on a word (type, schema, index, validation, template,
   private, library, log, transcript, plugin, connector), that word wins over one we invented.

## 3. The bounded contexts

| Context | Owner package today | Owns | Its contract | Not this context |
|---|---|---|---|---|
| Knowledge base | `engram` (memory, compile, retrieval, export, init) | The notes, their schema, the indexing that turns notes into pages, the indexed read surface, the tools that read and write notes, the export. | The schema, the commands `IndexKnowledgeBase`, `IngestPage`, `ExportOkf`, their events, the `api` database schema, the read tools, the authoring tools, the MCP server. | Which knowledge bases exist (Registry of knowledge bases). Measuring retrieval (Evals). |
| Sources | `engram.sources` and `providers` | Bringing outside documents in: one connector per host, the source queue, the watch lists, the mirror files, the sync. | The three ports and their entry-point groups, the commands `SyncMirrors` and `ScanWatchList`, their events, and the mirror file format, the one file-based contract the Knowledge base reads. | Deciding what a page means (Knowledge base). |
| Registry of knowledge bases | `studio` and `templates` | Which knowledge bases exist, where each lives, when each is refreshed, what its refresh runs did, the templates. | The HTTP API on port 8100, the settings file, the refresh job kind, a template folder. | Reading or writing notes (Knowledge base). Running workflows (Workflows). |
| Evals | `engram.evals` | Measuring retrieval against ground truth drawn from a knowledge base's own structure, and tuning the retrieval policy. | Dataset files, the evals commands and events, the policy it hands to the Knowledge base. | Writing into a knowledge base's folder (finding B2). |
| Workflows | `core.ai.workflows`, `harness.features.running`, `harness.features.scheduling` | The workflow as data: its nodes, its validation, its library, the trust it earns by running, and its execution: runs, gates, schedules, spend. | The workflow, the library protocol, the port verbs, the run and decide routes, the run evidence. | Who is chatting (Sessions). |
| Sessions | `harness` (session, domain.events, features.sessions, plugins, composition, selfauthoring) | One conversation with the agent: its record, its turns, its plugins, its delegates, and the custom items the agent writes for itself. | The session log, the session projection, the sessions routes, the agent-client protocol entry. | Executing a workflow (Workflows). A session launches one and records that it did. |
| Shared kernel | `core` (db, files, markdown, cqrs, observability, resolver, ai.retrieval contracts) | Building blocks with no domain in them. | The `Command`, `Event` and `UnitOfWork` types, the job runner, the change-notification channel, the retrieval message types. | Any domain word. |

AI Factory Studio (`studio-client`) is not a context. It is a client of every context and uses their words unchanged.

Two candidate contexts from the inventory were merged. Workflow authoring and running share one model and one codebase.
Compile and retrieval share one owner of one data set, and the authoring tools that live under the retrieval package
write notes and index them, so they are the Knowledge base's write contract, not a second context.

## 4. The vocabulary, per context

Each table defines the terms that anchor a context, gives one sentence that uses it (the stranger test), and names the
words it replaces. The inventory holds every other term.

### 4.1 Knowledge base

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Knowledge base | One body of markdown notes in one folder, the pages they index into, and the schema that governs them. | The research knowledge base holds 588 pages. | `KnowledgeBaseSchema`, `knowledge_base_root`, `kb` (short form in code and SQL) | corpus, vault, collection, registry entry, corpus root. Everywhere, code included. |
| Note | One markdown file in a knowledge base: typed frontmatter, sections, and for evidence types, claims. | The agent wrote a note about the paper and indexed it. | `write_note`, `read_notes` | memory note, unit, document. |
| Page | A note once indexed, what search and fetch answer with. | Search returned six pages from two knowledge bases. | `kb.pages`, `Page` | The saved workflow "page" and the reusable step "page" (4.5). |
| Page type | The type a note declares, from the schema's closed list. Already the `type` field of every note. | A note of type paper needs a summary and a findings section. | `page_type`, `page_types` | kind, page kind. |
| Page date | The day a page is about, which a search's date range reads. The schema's `retrieval.date_field` names the frontmatter field it is read from, `created` (the day the page was written) unless the schema names another. A page whose field holds no date has no page date and falls outside every date range. | The search kept the pages whose page date falls in March 2019, whatever day each was written. | schema key `retrieval.date_field`, `RetrievalSpec.date_field`, `kb.pages.page_date`, `IndexedPage.page_date` | created date (when a search range is meant). `created` stays the day a page was written. |
| Schema | The knowledge base's declared structure: page types, folders, fields, relations, statuses, guidance. | The schema says a flag page can be accepted, dismissed or completed. | `knowledge-base-schema.json`, `KnowledgeBaseSchema` | spec, corpusspec, ontology. A database schema is always "database schema". |
| Vocabulary | The knowledge base's allowed entity types, relations, facets and claim types, read from its vocabulary pages. | Validation refused the note because its relation is not in the vocabulary. | `VocabularyIndex`, `_meta/vocabulary/` | registry (when the vocabulary is meant), RegistryIndex, `_meta/registry/`. |
| Claim | One evidence bullet under a note's findings, with its quote and its location. | The claim quotes the paper's table and names the dataset. | `Claim` | claim block. |
| Locator | Where in the source a claim's quote sits, its `loc`: a section path such as `§4.1`, a page, a table, or the heading of the chunk the quote came from. | The claim's locator is `§7` and its quote is the authors' sentence. | `loc`, `is_locator`, `swapped_locator_quotes` | location (for a claim). |
| Index (verb) | Turning notes into pages: parse, resolve, validate, load, embed. Full or incremental. | Indexing took 14 seconds and embedded 90 units. | `IndexKnowledgeBase`, `studio index` | compile, build, rebuild. "Full index" replaces "full rebuild". |
| Run indexing | When a knowledge base's writes are indexed: after each write (the default), or once when the workflow run that made them ends, whether it succeeded or failed. The knowledge base's schema chooses. | With run indexing once per run, the digest run indexed its fifty new pages once at its end. | schema key `run_indexing` (`each_write`, `once_per_run`), `KnowledgeBaseSchema.run_indexing`, `RunScopedReindex`, `RunIndexing`, `hold_run_writes()`, `ReindexResult` outcome `deferred` | |
| Validation | The checks a note must pass. Errors block the note, warnings inform. | Validation found one dangling link and no errors. | `ValidationReport`, `validation_store`, `ValidationReportSink`, `FileValidationReportSink` | lint, check, ingest gate. |
| Validation finding | One validation error a page stands with, by rule name, as the index last stored it or a reindex just reported it. | The digest arrived with two validation findings, both quote-not-in-source. | `findings` (a `compounding_work` row field), `ReindexFinding`, `kb.validation_store` | finding (bare). An inspection finding stays the inspector's own word. |
| Read tools | The four tools an agent reads a knowledge base with: search, fetch, expand the graph, find paths. | The read tools refuse a search that names no knowledge base. | `search`, `fetch`, `graph_expand`, `find_paths`, `studio_read_tools`, `StudioReadToolsOpener` | doors (when these are meant), retrieval surface. |
| Reach (graph) | How far a set of seed nodes reaches in a knowledge base's link graph with no hop limit: the nodes reached, the hops to the farthest and to a target, and whether the walk took in every node it could. | The reach says the end lies five hops out, past the four the route search looked. | `api.graph_reach`, `GraphReach`, `reach` (a graph result field), `GRAPH_REACH_NODE_LIMIT` | |
| Envelope | What one leveled read returns: the unit's ref, kind, title, summary, content at this level, one-line stubs of its children and the mode it was read under. Also the MCP `fetch` output and the `GET /api/envelope` body. | The envelope of the paper lists four section stubs. | `DisclosureEnvelope`, `ChildSummary`, `ProgressiveDisclosure` | disclosure payload. |
| Disclosed result | A tool result an agent sees as a ref and a one-line summary in place of the full record, which it loads by ref when the summary is not enough. | The search returned six disclosed results and the agent loaded one. | `ResultDisclosure`, `Binding`, `load_result` | DiscloseResults. |
| SQL tools | The two tools that read the indexed database directly, when the owner turns them on. | With the SQL tools on, the agent ran one SELECT over the pages. | `run_sql`, `describe_schema` | SQL door. |
| Authoring tools | The tools an agent writes a knowledge base with: write, check, read, resolve, request a source, decide a page. | The authoring tools refuse a note whose page type the schema does not declare. | `AuthoringTools` | authoring doors, the eighteen-tool bundle. |
| Entity resolution | Turning each name a page mentions into the id of the one entity page for it. A name matches an existing entity page when it equals that page's title or one of its aliases once case, punctuation and a leading "the" are set aside. A name that matches none resolves through the first of its aliases that does, and gets a new page when none does. The schema names the entity page type. | Entity resolution matched "the Verge" to the existing page for The Verge and created a page for Sam Altman. | `resolve_entities`, `EntityName`, schema key `entities.page_type`, `EntitiesSpec` | |
| Guidance | Prose that tells an agent how to fill or use a thing: a page type's guidance, a section's guidance, the retrieval guidance. | The page type's guidance says what a good summary contains. | `guidance` | doctrine, voice, instructions (when attached to a knowledge base). |
| Decision | A page's move from its open status to one of its outcomes, made by a person or a workflow. | The owner's decision dismissed the flag. | `decide_page`, `decision` | flag (as a verb), flag_decide. |
| Skip (a digest) | Set status skipped on every digest page of one source, with the reason noted on each page. The pages and the mirror stay, and the source counts as digested. | The repair skipped the off-purpose paper. | `skip_digest`, status `skipped`, `decision_reason` | nothing |
| Defect | A page the pipeline wrote that misstates its own source or falls outside the knowledge base's purpose, fixed by changing that page alone. | The flag pass named the physics digest a defect, off purpose. | `defects`, `defect` (`off_purpose`, `misquoted`, `mislabelled_claim`, `empty`, `duplicate`, `remove_source`) | risk (for a defect in our own page). |
| Digest repair | Fixing a defect in the run that found it: skipping the source's digest pages, rewriting their claims, or rewriting the pages, never a flag for the owner. | The digest repair rewrote five section pages and skipped one digest. | `repair_digest`, `list_repairs`, `repair_sources`, `count_owner_repairs`, outcome `repaired`, `failed`, `needs_owner` | repair (bare). What a rejecting gate asks for stays the gate's requested repair. |
| Mirror | The on-disk markdown copy of one source, under the knowledge base's sources folder, kept in sync with the host. | The mirror of the paper is 40 pages of converted PDF. | `Sources/`, `MirrorFile` | unchanged. The evals "drafting mirror" becomes "drafting text" (4.4). |
| Chunk | One piece of a mirror's own text that the index stores, embeds and searches by itself, with the heading and section path it sits under. A raw hit is one chunk. | The answer sat in the report's ninth chunk, far from its opening. | `MirrorChunk`, `chunk_mirror`, `kb.raw_chunks`, `chunk_count` | passage (for a chunk). Passage is an evals archetype (4.4). |
| Heading section | The text of a mirror under one heading, up to the next heading, or the text before the first heading when it holds any. A mirror with no heading is one heading section. | The paper's methods heading section became three chunks. | `section_count` (a `read_source` answer field), schema key `sources.digest_sections_over`, `heading_sections_for_planner` | section (bare, for a mirror). A page's section stays a section. run (for a stretch of text, run is an execution). |
| Chunk sizes | How many tokens a chunk aims for (the target, 400 by default) and may hold (the maximum, 800 by default). | With chunk sizes of 400 and 800, the long article became nine chunks. | `ChunkSizes`, `DEFAULT_CHUNK_SIZES`, settings `ingestion.chunk_target_tokens` and `ingestion.chunk_max_tokens` | |
| Chunking | How the index cuts a mirror into chunks. It cuts at the headings, and each heading section that fits the maximum chunk size is one chunk, so a mirror with no heading that fits stays whole. Longer text of several paragraphs, up to the planner ceiling, is cut where the chunk planner says its topic changes, and a chunk of it over the maximum is filled. Any other longer text is filled: whole paragraphs go into a chunk until the next would pass the target, a paragraph over the maximum is cut at its sentence ends, a sentence over it at its line breaks, and a line over it at the maximum. Every chunk of a heading section keeps its heading and section path. | Chunking kept the short abstract whole and filled the long article into nine chunks. | `chunk_mirror` | packing (pack only distils a session). |
| Chunk planner | The model that chooses where a heading section changes topic. It is shown the heading section as numbered paragraphs and answers the numbers of the paragraphs where a new chunk starts, never text, so every chunk stays the mirror's own text. Its identity names its model, prompt version and thinking level. It is off until the settings name a deployment. With no chunk planner, or when it fails, is late, or answers numbers that are not strictly increasing paragraph numbers, the heading section is filled. | The chunk planner cut the live blog where each update began. | `ChunkPlanner`, `ChunkPlanner.identity`, `PydanticAiChunkPlanner`, `PydanticAiChunkPlanner.identity_for`, `CutPlanStage`, `FakeChunkPlanner`, settings `ingestion.chunk_planner_model`, `ingestion.chunk_planner_deadline_seconds` and `ingestion.chunk_planner_concurrency` | planner (bare, for this model). |
| Planner ceiling | The most tokens of a heading section the chunk planner is shown, 12,000 by default. A longer heading section is filled. | The year-long tracker passed the planner ceiling, so it was filled. | `DEFAULT_PLANNER_CEILING_TOKENS`, setting `ingestion.chunk_planner_ceiling_tokens` | model ceiling. |
| Cut plan | The paragraph numbers a chunk planner chose for one heading section, stored by knowledge base, a hash of the section's text and the cut plan identity: the chunk planner's identity with the version of how paragraphs are numbered. Indexing the same text again under the same cut plan identity reuses it and calls no model. An index report counts the cut plans it reused and chose, and the heading sections it filled because the chunk planner gave no accepted answer. | The second index reused every cut plan and called the model for none. | `kb.cut_plans`, `CutPlans`, `CutPlanQueries`, `SqlAlchemyCutPlans`, `FakeCutPlans`, `MirrorDoc.cut_plans`, `cut_plan_identity`, `PARAGRAPH_NUMBERING_VERSION`, `CutPlanCounts`, `IndexReport.cut_plans` | |
| Decision model | A model that answers typed questions about a text with a probability and writes no text. pydantic-ai's term. | The decision model judged nine of the twelve hits relevant. | `DecisionModelQuestions`, `decision_model`, `FakeDecisionModel` | unchanged. |
| Re-rank | Put a search's hits in the order a decision model judges them likely to help answer the query, dropping none. | The re-rank moved the restart page to the top. | `RerankedSearch`, `Reranking`, `reranking`, `retrieval.rerank` | unchanged. |
| Search tuning | The numbers search ranks with, kept in `kb.retrieval_tuning`: one default row for the whole database and at most one row for each knowledge base. A knowledge base's row may set only the cap on hits per document and the folded snippet length, and what it sets wins over the default row for that knowledge base's hits. A session that cannot see a knowledge base reads the default row's cap for it. Every other number comes from the default row, and a number no row sets is the default the search function carries. | The owner raised the cap to two in one knowledge base's search tuning, and every other knowledge base kept one. | `kb.retrieval_tuning`, `SearchTuning`, `SearchTuning.partition_col`, `SearchTuning.partition_visible` | |
| Document (of a search hit) | What one search hit stands for: the source its unit came from when it came from one, or else its page. A page made from a source, its sections, its claims and the source's chunks are one document, and an entity page made from no source is a document of its own. Search keeps at most `max_hits_per_document` hits of each document, its best first, as the search tuning sets it for the document's knowledge base: 1 unless the owner sets another, and every hit when the owner sets 0 or null. A document is never a note. | The top five hits were five documents, so the long report's chunks no longer crowded out the other sources. | `SearchSurface.document_expr`, tuning key `max_hits_per_document`, `SearchTuning.max_hits_per_document`, `api.search_hits_per_document(collection)`, `document_clause` | |
| Folded unit | A matching unit of a hit's document that search left out of the ranked list and lists under that hit instead, best first, with its id, its hit level, a short snippet of the text it matched and the ref that fetches it. The snippet is the matched text, cut to the folded snippet length the search tuning sets for the knowledge base when it is longer, 160 characters unless the owner sets another, ending in an ellipsis when cut, and is never longer than a hit's snippet. | The digest's card came back with a section and a chunk as folded units, and the chunk's snippet held the sentence the agent needed. | `folded_units` (a search hit field and an `api.search` column), `FoldedUnit`, `FoldedUnit.snippet`, `FOLDED_UNITS_PER_HIT`, tuning key `folded_snippet_chars`, `SearchTuning.folded_snippet_chars` | |

### 4.2 Sources

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Source | One outside thing engram reads: a paper, a video, a post, an article. | The owner requested a source and the digest workflow wrote its note. | `Source`, `request_source`, `Sources/` | unchanged. |
| Connector | The code for one host that can claim a URL, read a source from it, and say its source type. A person never meets it, they name a source or a watch-list entry. | The X connector reads the account, the Zotero connector reads your library. | `Connector`, group `engram.sources.connectors`, schema key `sources.connectors` | provider, source adapter, source reader. Provider stays pydantic-ai's word for a model vendor. |
| Converter | Code that turns one document format into markdown. | The PDF converter fell back to OCR. | `DocumentConverter` | unchanged. |
| Watch list | The list of accounts, feeds and pages a knowledge base keeps an eye on for new sources. One entry names what is watched. | Tell engram which X account to watch, and it joins the watch list. | `watch-list.yaml`, `WatchEntry` | feed, feeds file. |
| Scanner | The part of a connector that reads one kind of watch-list entry and answers the new sources it found. | The X scanner found four posts since yesterday. | `Scanner` | feed scanner. |
| Source queue | The list of requested sources and what happened to each: requested, resolved, failed, skipped, seen. | The source queue shows the URL was requested by the agent and resolved as a paper. | `SourceQueue`, `request_source` | intake, intake ledger. |
| Source type | What a requested source turns out to be: video, paper, post, article. | The source type of that URL is video. | `SourceType` | medium, kind, IntakeKind. |
| Source metadata | The facts a connector reports about one source beyond its title and dates, kept as text by name. They are stored in the source row, and the schema's `sources.digest_fields` copies them to digest fields. | The connector put the region in the source metadata, and the digest skeleton filled the region field from it. | `SourceItem.meta`, `MirrorFrontmatter.meta`, `kb.sources_raw.meta` | |
| Outlet | The publication a news article appeared in, such as TechCrunch or The Verge. A word of the news-benchmark template only. engram, the studio and the harness never use it. Its connector passes it in a source's metadata, and the template's schema maps it to a digest field. | The digest says the article's outlet is TechCrunch. | `outlet` (a digest field and a facet of `templates/news-benchmark/`, a metadata key of the `multihop_rag` connector) | source, the dataset's own name for it, because a source is the outside thing engram reads. |
| Resolver | The code that turns one requested source of one source type into markdown for its mirror. It fetches, converts and titles the source, or refuses with the reason. | The paper resolver fetched the PDF and the converter turned it into markdown. | `Resolver`, `ResolveError`, `VideoResolver`, `PaperResolver` | unchanged. |
| Sync | Bringing the mirror files level with every connector. | The sync wrote three new mirrors and skipped 200. | `SyncMirrors`, `studio sync` | unchanged. |
| Source identity | The identifiers that name one source whatever connector brought it: its URL, its arXiv id and its DOI. A sync admits no new mirror whose source identity an active mirror under another key shares. | The library item was not admitted, its arXiv id was already mirrored from the source queue. | `source_identities`, `mirrors_by_identity`, `holder_of`, `MirrorFile.url`, `MirrorFile.doi`, `SyncReport.already_mirrored` | |

### 4.3 Registry of knowledge bases

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Registry of knowledge bases | The settings-file list of every knowledge base the studio knows: name, folder, description, refresh cadence, private or shared. | The registry of knowledge bases lists four. | `ApplicationSettings.knowledge_bases`, `registered_knowledge_bases()`, `KnowledgeBaseRegistry` | registry (bare), corpora, registry_targets. |
| Registered knowledge base | One row of that registry. | The registered knowledge base notebook is private. | `KnowledgeBaseEntry`, `RegisteredKnowledgeBases`, `RegisteredKnowledgeBaseQueries` | registry entry, registry row, target. |
| Refresh | One background pass over one knowledge base: scan the watch list, sync the mirrors, place new notes, index. | The nightly refresh of research took nine minutes. | job kind `kb.refresh`, `studio refresh`, `KnowledgeBaseRefresher`, `StudioKnowledgeBaseRefresher` | ingest job, corpus job, ingest. |
| Refresh run | One execution of a refresh, with its outcome and its report. | The last refresh run failed on a broken mirror. | `RefreshRun` | ingest run, RunRecord (studio). |
| Refresh counts | What one refresh run brought in: the sources its sync created, the new sources each watch-list entry put on the source queue, and the sources each connector created. | The refresh counts say six sources came in, all found by one X account. | `RefreshCounts`, `sources_created`, `requested_by_entry`, `created_by_connector` | nothing |
| Harness forward | The studio subscriber that posts each completed refresh run, and each abandoned one that created sources, to the harness, which tells the owner and arms the schedules waiting on that knowledge base. | The harness forward retried the noon refresh run until the harness came back. | `harness_forward`, `HarnessRefreshForward`, `POST /api/schedules/refresh-runs` | nothing |
| Librarian pass | The optional step of a refresh, after the sync and before the index, where an agent moves each newly landed mirror into a folder under `Sources/`. | The librarian pass moved the new paper into Papers/NLP. | `LibrarianPlacer`, `sources.librarian` | unchanged. |
| Note retention | Keeping a value a workflow run produced as a note in a registered knowledge base. A preview shows the note and writes nothing, and retaining it publishes the note at a name nothing holds. | Note retention kept the run's summary as a concept page. | `NoteRetention`, `previewNote`, `retainNote` | unchanged. |
| Retention receipt | The file that records how far one retention request has got, so a retry meets its own earlier outcome. | The retention receipt says the note was saved but not indexed. | `RetentionReceipt`, `RetentionReceipts`, `.engram/retention/` | unchanged. |
| Background worker | The process that claims refresh jobs, ticks the cadences, and delivers notifications. | The background worker is down, so nothing indexes. | `studio worker` | corpus worker, refresh worker. |
| Job | One unit of durable work in the jobs table: its kind, its key, its payload, its state and its attempts. A job runner claims it, runs it, and marks it done, failed or abandoned. | The refresh job failed twice and waits for its third attempt. | `Job`, `Jobs`, `JobQueries`, `coordination.jobs` | job store (for the port). |
| Outbox | The table that holds the events a transaction emitted until a job runner has delivered them to every subscriber. | The outbox still holds two events nobody has delivered. | `Outbox`, `coordination.outbox`, `coordination.event_deliveries` | outbox store, delivery ledger. |
| Template | The folder of starting files a new knowledge base is created from: schema, guidance pages, workflows. | A new knowledge base started from the research template. | `templates/<slug>/`, `studio init --template` | bundle. The export directory is never a template. |
| Private | A knowledge base that no session reads unless it is opened inside that knowledge base's own folder. The opposite is shared, the default. | The notebook knowledge base is private. | `private: true` | library visibility, `library: false`, `retrieval.library`. |

### 4.4 Evals

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Evaluation run | One scored pass of a dataset by one agent variant. | The evaluation run scored recall at 0.65. | `evaluation_runs`, `engram evals run`, `EvaluationRunConfig`, `EvaluationRunReport` | eval run. |
| Agent under test | The agent an evaluation run scores: one model, its instructions and a retrieval toolset, answering with the page ids it cites. | The agent under test cited three pages. | `AgentUnderTest`, `build_agent` | unchanged. |
| Experiment | A set of evaluation runs that holds one axis constant (model, instructions, dataset or retrieval strategy) and varies the others. Each combination is a variant. | The experiment held the model and varied the strategy. | `ExperimentAxes`, `ExperimentVariant`, `run_experiment` | unchanged. |
| Trial | One guarded run of one case, whose tool results are stored as delivered evidence and whose steps are traced. | The trial stored four tool results. | `trial_id_for`, `BlindTrialRunner` | unchanged. |
| Evaluation case result | The scores one case earned in one evaluation run, with its tool calls, tokens and duration. | The evaluation case result shows recall at 1 and four tool calls. | `evaluation_case_results`, `EvaluationCaseResultRow` | eval case result. |
| Anchor page | The page a generated case was authored against. Its id names the case. | The anchor page of that case is the retrieval survey. | `QaCaseMeta.anchor_page_id`, the dataset key `paper_id` | paper id (for a case). |
| Dataset | An immutable, versioned file of question and answer cases with their expected pages. | Dataset v3 has 120 cases. | `<name>.v{N}.yaml`, `EvaluationDataset`, `EvaluationCase` | golden dataset. |
| Benchmark dataset | A published dataset whose queries people wrote and whose relevance people judged, such as SciFact. | SciFact is a benchmark dataset of 300 test queries. | `beir` connector, `benchmark-dataset` template | benchmark (alone). |
| Outcome benchmark | A governed comparison of workflow outcomes across arms on frozen cases, scored by graders, in the harness's outcome-evaluation slice. Never shortened to benchmark. | The outcome benchmark compared two workflow versions on twelve cases. | `run_outcome_benchmark`, `BenchmarkRequest`, `BenchmarkRecord` | benchmark (alone). |
| Relevance judgment | A person's grade for how relevant one document is to one query, 0 meaning not relevant. | NFCorpus gives that document a relevance judgment of 2. | `QaCaseMeta.judgments`, `judgments.<split>.jsonl` | qrel. |
| nDCG | Normalized discounted cumulative gain: how close a ranking comes to the ideal order of the judged documents, from 0 to 1. | nDCG@10 is the official SciFact metric. | `ndcg@10`, `ndcg_at_k` | |
| Passage | The archetype drawn from one section of one page whose text has at least 400 characters. The model writes a question that section answers, and the gold is that section's page. It needs no claims and no links, so any knowledge base can give one. | The passage case asks what the setup section says about the batch size. | `Archetype.PASSAGE`, `passage_seeds`, the seed key `passage:{section_id}` | |
| Search setup | One named way to run search with no agent: hybrid or keyword-only ranking, over every hit level or over one. It sets only those two search knobs. It is neither a retrieval policy nor an agent's retrieval strategy, and it never changes the knowledge base's search tuning. | The keyword setup scored SciFact lower than the hybrid setup. | `SEARCH_SETUPS`, `SearchSetup`, `search_setup`, `ScoreDataset.setup`, `studio evals score --setup` | |
| Generator check | The check that a generated dataset ranks the search setups the way a human-judged dataset of the same knowledge base does. Both are scored under every search setup, the setups are ranked by nDCG@10 within each, and the check passes only when both rankings are the same (Kendall's tau-b of 1). | The generator check passed, so the generated dataset can stand in for human judgments. | `generator_check`, `GeneratorCheck`, `CheckGenerator`, `GeneratorChecked`, `studio evals check-generator` | |
| Query type | The type of query a benchmark case asks, as the benchmark labels it, for example `comparison_query`, `inference_query`, `temporal_query` or `null_query`. Scores are also reported per query type. | Answer accuracy was lowest on the temporal query type. | `QaCaseMeta.query_type`, the judgments-file key `query_type`, the results-file key `query_type` | question type, the benchmark's own name for it, because question type is the inspector's word (4.5). |
| Answer accuracy | The share of an evaluation run's cases whose answer matches the case's expected answer. Strict accuracy compares the two after dropping text inside square brackets or inside 【 and 】, then lowercasing them and dropping punctuation and the words a, an and the. Official accuracy uses the benchmark's own word-overlap rule. No model grades an answer. | Strict answer accuracy rose from 61 to 68 percent with the SQL switch on. | `AnswerAccuracy`, `answer_strict`, `answer_official`, `normalize_answer`, the results-file keys `strict` and `official` | |
| SQL switch | The option of an evaluation run that gives the agent under test the SQL tools, so it can read the database schema and filter pages with SQL. Off by default. | With the SQL switch on, the agent filtered the pages by date before it searched. | `studio evals run --sql`, `EvaluationRunConfig.sql`, `EvaluationSqlTools`, the results-file header key `sql` | |
| Code mode | Driving an agent's tools through one sandboxed `run_code` tool: the agent writes Python that calls the tools as functions, the SQL tools included, instead of calling each tool itself. An evaluation run turns it on with `--code-mode`. Off by default. Its tool-call budget counts each `run_code` call once, like any tool the agent calls itself, and never the tool calls the program makes. | With code mode on, the agent ran three searches in one `run_code` call. | `studio evals run --code-mode`, `EvaluationRunConfig.code_mode`, `CodeMode`, `ModelCallsCodeMode`, the results-file header key `code_mode` | |
| Thinking level | How much a model reasons before it answers, one of low, medium, high or xhigh. It is set when the model is built and sent with every request, and a Responses API model reads it as its reasoning effort. Each model tier carries one, and an evaluation run may set one for the model under test. Unset, the model runs at its own default. | The baseline and the candidate both ran at thinking level xhigh. | `Thinking`, `TierSetting.thinking`, `studio evals run --thinking`, `EvaluationRunConfig.thinking`, the results-file header key `thinking` | Not the reasoning-effort directive, the sentence `apply_reasoning_effort` adds to an evaluation run's instructions. |
| Pinned search | A search tool fixed to one knowledge base, with no `collection` argument, that ignores any other knowledge base the agent names. It keeps its date range and its facets. An evaluation run uses it so the agent under test stays in the dataset's knowledge base. | Every evaluation run in the comparison used a pinned search. | `studio evals run --pinned-search`, `build_retrieval_toolset(pinned=)`, `EvaluationRunConfig.pinned_search` | |
| Results file | The per-case JSONL file an evaluation run writes when asked: a header line that records how the run was set up, then one line per case with its answer scores, its citations, its retrieval scores, its other-collection flag and whether it was a transient failure. The header records the strategy, the SQL switch, code mode, the model, its thinking level, the instructions, the tool-call budget, the search pinning, whether search took a date range, the dataset, the knowledge base, its index build and the source scoring version its lines were scored under. A failed case is written as incorrect. A file missing a key the run writes predates the current format and is refused. A resumed run rewrites the file under the earlier header. | The results file shows the case failed on its tool-call budget. | `studio evals run --results`, `EvaluationRunConfig.results_path`, `ResultsHeader`, `ResultsLine`, `results_lines`, `write_results`, `read_results`, `dataset_sha256`, the header keys `code_mode`, `thinking`, `search_date_range`, `index_build` and `source_scoring_version`, the line keys `transient`, `seen_recall` and `seen_gold` | |
| Transient failure | A failed case whose error came from the model's connection or service or from the database connection, not from the agent under test: a model connection error, a model request timeout, an HTTP 429 or 5xx from the model, or a database connection failure. A tool-call limit, a refused answer, an error the agent's own SQL statement causes and every other error are not transient. | Thirty-six cases were transient failures, all request timeouts in one burst. | `TRANSIENT_STATUS_CODES`, `TRANSIENT_MODEL_MESSAGES`, `is_transient_failure`, `ResultsLine.transient`, the results-file key `transient` | |
| Database connection failure | An error that reports a connection to the knowledge base's database that was refused, could not be opened or closed under a statement, or a database shutting down or starting up. It is matched by the error's type, raised alone or wrapped by SQLAlchemy, and by asyncio's text for a failed connection attempt. An error the statement itself causes, such as a syntax error, an undefined column, a refused permission or a statement timeout, is not one. | The resumed run ran the 909 database connection failures again after the database came back. | `DATABASE_CONNECTION_ERRORS`, `is_database_connection_failure` | |
| Service connection failure | A failure whose connection to a service failed: an embedding endpoint or a model endpoint, named by its host, or the database, named by its URL with the credentials removed. Its message names the service before the error's own text. An embedding endpoint is an OpenAI-compatible endpoint whose request path ends in `/embeddings`, and any other is a model endpoint. | The score failed on a service connection failure: embedding endpoint example.openai.azure.com: Connection error. | `connection_failure_message`, `ServiceConnectionError` | |
| Resumed run | An evaluation run that continues the results file of an earlier run of the same variant. It keeps every case line that was not a transient failure, runs only the cases the file lacks and the transient failures, and writes one file holding both under the earlier header, in dataset order. It refuses a file whose header differs from its settings in a field the paired comparison holds, whose index build differs from the one the run reads, a header with no index build counting as unknown, or whose source scoring version differs from the one the code scores under, before any case runs. A changed knowledge base fingerprint is accepted with a warning, and a source lookup that fails leaves only the cases run now without source scores. With no file there yet, it runs every case. | The resumed run kept 364 cases and ran the 36 transient failures again. | `studio evals run --resume`, `EvaluationRunConfig.resume`, `ResultsFileNotResumableError` | |
| Source-level retrieval | Retrieval scored against the source documents the cited pages and chunks came from, not against the cited page ids. A cited ref is read as the bare unit id it names, a cited page maps to the source its page record names and a raw chunk to the source it was cut from, and recall@4, recall@10 and MRR compare those sources with the case's gold source ids. Each citation holds one rank: a source counts once, at its first citation, and a citation with no source or a repeat of a source cited above it matches nothing. `studio evals score` reads a search's hits by the same rule. | Source-level retrieval found the evidence document at rank two. | `studio evals run --source-level`, `EvaluationRunConfig.hit_sources`, the results-file keys `sources`, `recall@4`, `recall@10` and `mrr`, the header keys `source_lookup_error` and `source_scoring_version`, `SOURCE_SCORING_VERSION` | |
| Seen recall | The share of a case's gold sources that any tool result the model received during the case named, read from the run's own messages. A source counts as seen when a result carries something of one of its units (a page, a section, a claim, a raw chunk or the source itself), never when it carries only a bare id that points at one. Units map to sources the way source-level retrieval maps citations, so it is measured only beside it. A search names its hits and every folded unit that carries a snippet, a fetch the unit it read and every child it lists, a graph answer its nodes, and a SQL row its `id`, `page_id`, `section_id`, `claim_id` and `source_id` columns, never a relation end such as `src_id` or `dst_id`. A unit's text is a hit's snippet, a folded unit's snippet, a fetched body, a leveled fetch's content, a child's summary or a SQL row's `text`, `quote`, `snippet` or `body`, and a text holding only the ellipsis of a cut snippet is no text. A claim counts only when its text came with it, never as a bare claim id or a provenance pointer. In code mode it is the `run_code` output the model reads: a unit that any program tool call of the case returned counts when an output carries the first 60 characters of its text, or, once every returned text the output carries is cut out, its id, ref or page id as a word. A failed case keeps what it saw before it failed. | The case cited no gold source, yet its seen recall was 1, so the agent read the evidence and did not cite it. | `seen_units`, the results-file keys `seen_recall` and `seen_gold`, `SourceLevelRetrieval.seen_recall`, `PairedScores.seen_recall_difference` | |
| Index build | What identifies the index a knowledge base's files were built into: the stored indexer version, which carries the chunk sizes and the chunk planner, the embedding model, the vocabulary hash and the retrieval switches that are on. Two runs over the same files read different index builds before and after the chunk sizes change, while their knowledge base fingerprints agree. A knowledge base never indexed has the index build `not indexed`. | The two runs share a fingerprint but not an index build, because the candidate ran after the chunk sizes changed. | `SupportsCoverageReader.index_build`, the results-file header key `index_build` | |
| Other-collection flag | The mark on a case whose SQL statements returned a row from another knowledge base, read from the row's collection columns. A statement that reads only the views describing every knowledge base, such as `api.about`, never sets it. | Two cases carried the other-collection flag, both from a join on edges. | the results-file key `other_collection`, `ResultsLine.other_collection` | |
| Paired comparison | Comparing two evaluation runs of one dataset case by case: the difference in strict answer accuracy with its 95 percent interval, McNemar's exact test on the cases only one run answered correctly, and both per query type, beside each run's failed cases, its mean source-level retrieval and its mean seen recall with the paired difference, missing for a run whose results file holds none. It refuses two runs whose dataset, model, thinking level, instructions, tool-call budget, search pinning, search date range or knowledge base differ, and two runs that both carry source-level scores under different source scoring versions, naming the run to run again. Two runs under one strategy may differ in the SQL switch or in code mode, never in both, and two runs under different strategies must share both. The report shows each run's SQL switch, code mode, thinking level, knowledge base fingerprint and index build, and two runs may differ in the last two. When one call compares several pairs, Holm's method adjusts their p values. | The paired comparison found that the SQL switch added seven points of answer accuracy. | `studio evals compare`, `engram.evals.paired_comparison`, `--pair`, the header keys `search_date_range` and `source_scoring_version` | |
| Stratum | One cell of a dataset's coverage: an archetype crossed with a difficulty band, and for a knowledge base census also an anchor page type. Draws and carves take cases stratum by stratum. | The survey hard stratum holds four cases. | `Stratum`, `case_stratum`, `carve_strata` | unchanged. |
| Retrieval policy | The tuned instructions and strategy a knowledge base's read tools speak with. | The retrieval policy tells the agent to scan cards before reading sections. | `RetrievalPolicy` | active policy, applied policy. |
| Tuning run | One pass of the loop that tunes a retrieval policy against a dataset and accepts a candidate only when it measurably improves. | The tuning run accepted the second candidate. | `TuneRetrieval` | calibration, calibration run, calibrate. |
| Decision core | The fixed stratified subset of a tuning run's train cases that every accept decision is measured on. It is carved once per run from a recorded salt. | The candidate beat the incumbent on the decision core. | `carve_core`, `CoreTail.core`, `core_size`, `core_salt` | unchanged. |
| Rotating tail | A fresh stratified sample of the train cases outside the decision core, drawn each round of a tuning run. It feeds the diagnosis and never decides an accept. | The rotating tail showed the candidate doing worse on cases outside the core. | `draw_tail`, `CoreTail.pool`, `tail_size` | unchanged. |
| Recommendation | A proposal to change a knowledge base's schema, with its evidence, never applied by the machine. | The recommendation proposes a new relation between papers. | `Recommendation` | advisory, structural advisory, advise. |
| Drafting text | The exact source text a case's quotes were checked against when the case was drafted. | The drafting text moved, so the case is stale. | `drafting_text` | drafting mirror. |
| Quote check | The check that keeps a generated case only when its quotes are verbatim in the source. | The quote check dropped two cases. | `accept` in `domain/drafting.py` | faithfulness gate. |
| Freshness check | The check of a dataset against the knowledge base as it is now, and the block on auto-applying a policy when too much is stale. | The freshness check blocked the apply at 12 percent stale. | `freshness`, `apply_staleness_max` | freshness sweep, machine staleness gate, staleness refusal. |
| Trial log | The append-only file of one outcome trial's steps. | The trial log shows the grader read three pages. | `TrialLog` | trial journal. |
| Budget record | The persisted admission record one evaluation cycle spends through. | The budget record refused the attempt because a charge was unresolved. | `EvaluationBudgetRecord`, `FileEvaluationBudgetRecord` | budget ledger. |
| Review authority | Who reviewed a label: the owner, an agent, a deterministic check, or nobody. | The case's review authority is the owner. | `ReviewAuthority` | review (bare). |

### 4.5 Workflows

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Workflow | A typed graph of nodes, saved as YAML, that an agent or a person wrote and the engine runs. | The digest workflow has five steps and one gate. | `Workflow`, `wf.<name>.v<n>` | spec (for a workflow), artifact, WorkflowSpec. |
| Step | One named unit of work a node performs. Registered on the host, or written inline in the workflow. | The relate step links a digest to the pages it builds on. | `StepRegistry`, `Step`, `StepName`, `StepExecutor`, `referenced_steps` | leaf, work, task, registered step. |
| Inline step | A step written inside the workflow itself: a briefing, a program, or an agent step. | The workflow's inline steps run on a host that never registered them. | `inline`, `Briefing`, `Program`, `AgentStep`, `CodeRunner`, `ToolCaller`, `AgentRunner`, `AgentResolver`, `InlineStepCall`, `InlineStepOutcome` | brought step, carried step, TaskSpec, CodeSpec, WorkerSpec. |
| Reusable step | A step kept in the library under its own name and version, copied into a workflow by naming it. | The workflow names the reusable step finish, version 2. | `st.<name>.v<n>`, `search_steps`, `fetch_step`, `ReusableStepQueries` | shelf step, step page, stored step. |
| Library | Where saved workflows and reusable steps are kept. Already the Studio's word for it. | The library holds nine workflows and thirty reusable steps. | `WorkflowLibrary`, `fetch_workflow`, `SupersessionIncompleteWarning` | store, shelf (all meanings), WorkflowStore. The set of readable knowledge bases is never "the library" (4.3 Private). |
| Workflow card | The words a person reads to choose a saved workflow or reusable step: title, summary, applicability, the author's own words and the intent. | The workflow card says what the digest workflow is for and when to use it. | `SavedWorkflow`, `fetch_card`, `WorkflowCardPatch`, `redescribe` | nothing. |
| Pack | Distilling a session into a workflow: reviewing what the conversation did and drafting a new workflow when it is worth keeping, or adapting an existing one. The workflow's counterpart of compacting a conversation. | After the long session, pack turned the six manual steps into one workflow. | `pack_session` (new tool, section 7) | pack as "store a draft", pack as the CLI loader. |
| Save | Storing a workflow that validates. The agent proposes, the person or the approval rule saves. | The person pressed Save and the workflow got version 3. | `save_workflow`, `save_draft`, `SaveRefusedError` | pack_workflow, pack_draft, pack (the act). |
| Import | Loading a folder of workflow and step files into the library, steps first, everything validated before the first write. | Import loaded the template's two workflows and nine steps. | `harness import`, `import_path` | `harness pack`, pack_path. |
| Workflow file, step file | The markdown file a workflow or a reusable step is read from and written to. | The step file carries the briefing under a YAML header. | `harness/workflows/{steps,workflows}/` | block, artifact file, blocks folder. |
| Node | One position in the workflow graph: run a step, map a step over a list, or a gate. | The map node runs the digest step once per source. | `RunNode`, `MapNode`, the gate nodes | grammar (bare). |
| Validation (workflow) | The checks a workflow passes before anything runs: wiring, types, reachability, and for a program, that it parses and imports only what the sandbox has. | Validation refused the workflow because one node's input is wired to nothing. | `validate`, `ValidationReport`, `IssueType`, `WorkflowInvalidError` | validate and prepare, cold checks. |
| Build (workflow) | Turning a validated workflow into the graph the engine runs. | The builder turns the workflow into a graph of five nodes and one join. | `build_graph`, `graph_builder.py` | compile, compiler, compiled (of a workflow). |
| Gate | The check of a step's outcome inside a workflow run. The run waits until the gate is decided by a person, by the orchestrator, or by a judge step. Nothing else is a gate. | The run is waiting at the gate for the owner's decision. | `PersonGate`, `OrchestratorGate`, `JudgeGate`, `GateDecision`, `ReviewedArtifactQueries` | gate for the ingest check, the quote check, the freshness check. HumanGate becomes PersonGate. |
| Gate mode | How a run's gates are decided: by a person, by the orchestrator, or passed automatically. | In orchestrate mode no person is asked. | `GateMode`, `effective_gate_mode` | auto gates. |
| Waiting | A run that has reached a gate and stopped until the gate is decided. Survives a restart. | Two runs are waiting at gates. | `waiting` | parked. |
| Abandoned | A run or a job that used up its attempts. Visible, never silent. | The refresh was abandoned after three failures. | `abandoned` | parked (for a job), dead. |
| Run | One execution of one workflow, from inputs to final value. Qualified when another kind of run is near: workflow run, refresh run, evaluation run, tuning run. | The workflow run finished in four minutes. | `run_workflow`, `run_id` | unchanged. |
| Run log | The file holding one run's recorded node outputs, so a waiting gate survives a restart. | The run log replayed the first three nodes after the restart. | `RunLog` | run journal. |
| Run archive | Where ended runs rest, one whole record per run, until a sweep removes the unpinned ones past their retention. | The run archive still holds last week's digest run. | `RunArchive`, `RunArchiveQueries`, `FileRunArchive`, `RunArchiveRecord` | unchanged. |
| Durable run | A workflow run the Temporal engine executes, which outlives the process that started it. | The durable run kept going while the harness restarted. | `DurableRunOutcome`, `durable_run_id`, `AddressProbe` | Attested, ledger run id. |
| Schedule | A standing intent to run one workflow on a cadence. A knowledge base has a refresh cadence, not a schedule. | The schedule fires the digest workflow every morning. | `Schedule`, `Cadence` | schedule (for a refresh). |
| Scheduler | The harness service that creates, changes and deletes schedules and fires the due ones on each tick. | The scheduler fired the digest schedule at nine. | `Scheduler` | schedule store (for the service). |
| Fire | One time a schedule comes due and starts its workflow, or is held. | The schedule's nine o'clock fire was held by the blackout. | `_fire`, `next_run_at` | unchanged. |
| Tick log | The fields that say what a schedule's latest fire did: when, its outcome, its next fire and its runner session. | The tick log says the last fire was missed. | `stamp`, `last_run_at`, `last_outcome` | unchanged. |
| Runner session | The session a schedule's fires run in, one per schedule. | The runner session still holds last night's run. | `session_id`, `_runner_session` | unchanged. |
| Gate policy | How long a scheduled run's gate waits for a person: forever, or a stated number of seconds. | The gate policy fails the gate after an hour. | `gate_policy`, `gate_wait_seconds` | unchanged. |
| Blackout | Daily quiet hours in which a schedule does not fire. | The blackout holds every fire between ten at night and six. | `blackout`, `BlackoutWindow` | unchanged. |
| Capacity key | A name schedules share to cap how many of their runs are live at once. | Two schedules share the capacity key, so only one runs at a time. | `capacity_key`, `capacity_limit` | unchanged. |
| Catch up | Firing once as soon as a held fire clears, instead of waiting for the next turn. | With catch up on, the held fire ran at six. | `catch_up` | unchanged. |
| On-refresh cadence | A cadence that fires a schedule once after each refresh run that brought new sources into the knowledge base the schedule's arguments select. Written `on refresh`. | The digest schedule's on-refresh cadence fired it after the noon refresh found six posts. | `OnRefreshCadence`, `on refresh` | nothing |
| After-run cadence | A cadence that fires a schedule once after a run of the named workflow ends succeeded or partial over a knowledge base the schedule also selects. Written `after <workflow name>`. | The flag schedule's after-run cadence fired it when the digest run ended. | `AfterRunCadence`, `after <workflow name>` | nothing |
| Arm | Make a resting schedule due now, because the refresh run or the workflow run its cadence waits for has happened. It fires on the next tick and rests again. | The digest run's end armed the flag schedule. | `arm`, `Armed`, `arm_after_refresh`, `arm_after_run` | nothing |
| Resting | A schedule with no next fire, which the clock never brings due. An arm makes one with an event cadence due, and a save gives one with a clock cadence its next fire again. | The flag schedule rests until the next digest run ends. | `next_run_at` is null | nothing |
| Environment | The steps, tools and capabilities a workflow runs against. A workflow is validated against its own environment, never against the orchestrator's. | The workflow's environment lacks the notify step, so it cannot run here. | `Environment`, `build_environment`, `WorkflowFingerprint.environment` | world, world stamps, work registry. |
| Fingerprint | The stamp of the environment a workflow was saved against, compared with the environment now. The orchestrator's model is not part of it. | The fingerprint says one step's contract moved since the save. | `WorkflowFingerprint`, `drift` | RunFingerprint. The index fingerprint stays qualified. |
| Evidence | What running has recorded about a workflow: which runs completed, what they produced, who approved it. | Two clean runs of evidence raised the trust level. | `RunEvidence` | attestation, attestation ledger. |
| Trust level | What the evidence has proven about a workflow: unverified, machine-confirmed, approved. | The workflow's trust level rose to machine-confirmed after two clean runs. | `TrustLevel` | trust tier. |
| Adaptation | A typed list of edits applied to a saved parent, with derived lineage. | The adaptation changed two briefings and kept the graph. | `adapt`, `Lineage` | unchanged. |
| Budget | What one step performance may spend: model requests and tool calls. Money caps are spend limits. | The step's budget is eight requests. | `Budget`, `SpendLimits` | unchanged. |
| Step conversation | The standing conversation one step keeps across its items and turns. | The digest step's conversation keeps every item's turns. | `StepConversation`, `StepConversations`, `StepConversationContinuation` | line, worker line. |
| Steer | A message a person sends a running step or turn, read before its next model request. | The owner's steer reached the digest step before its next request. | `StepSteerDelivery`, `steer_chunk`, `steer_inbox` | LineSteerDelivery. |
| Spend limit | One money cap a deployment holds runs to. | The run stopped at its spend limit. | `SpendLimit`, `spend_limits` | money brake, brakes (the wire field `brakes` stays). |
| Spend log | The append-only file of what each run spent. | The spend log shows the digest run cost two dollars. | `SpendLog`, `SpendLogQueries`, `FileSpendLog` | spend ledger. |
| Tick | One pass of a background loop: the scheduler, the refresh ticker, the inspection dispatch and sweep. | The scheduler's tick fired two schedules. | `tick` | once, tick_once. |
| Question type | What the inspector was asked: to attribute one lead, or to scan one whole run. | The question type of that question is scan. | `QuestionType` (the `questions.kind` column stays) | QuestionKind. |
| Collaborators | Collaborators are the services one entry hands down whole, so a launcher takes one parameter. | The run's collaborators carry its records, its topics and its run settings. | `RunCollaborators`, `SessionCollaborators`, `PilotCollaborators` | `PilotJourney`. |

### 4.6 Sessions

| Term | Meaning | In a sentence | Code names | Replaces |
|---|---|---|---|---|
| Session | One conversation with the agent: its record, its plugins, its runs. What AI Factory Studio lists. | The session has three turns and one waiting run. | `SessionStarted`, `session_id` | unchanged. `registry_session` becomes `library_composition`. A database session is always "database session". |
| Turn | One prompt and everything until the agent settles. | The turn called four tools and settled in 20 seconds. | `TurnStarted`, `TurnSettled` | unchanged. |
| Session log | The append-only file with one entry per fact about the conversation. The source everything else derives from. | The session log shows the turn started at 20:31. | `.state/session-log.jsonl` | session journal, journal (bare). |
| Log entry | One typed line of the session log. A fact, past tense, never edited. | The log entry says the gate was decided by the orchestrator. | `harness.domain.events` | journal event, journal line. |
| Transcript | The raw model messages of one turn, kept beside the session log by the step-persistence library. | The transcript holds the tool calls the summary hides. | `StepPersistence`, `Transcript`, `RecordPoolOpener` | step log, step store, model record, run (when this is meant). |
| Step entry | Which message one step of a transcript gave the model, in which form and which stamp version. Inside `core.ai.history` and `core.ai.transcripts` a step is one model request and its response. | The step's third entry is form 1 of the user's prompt. | `StepEntry` | `Entry`, entry (bare). |
| Plugin | One switchable piece of the agent the owner turns on or off: a set of tools and the instructions that go with them. | The web-search plugin is off in this session. | `Plugin`, `harness plugins`, `PluginSet` | door, capability (when the switchable unit is meant). Capability stays pydantic-ai's word for the building block a plugin wraps. |
| Always on | The plugins that cannot be turned off, listed so the whole set can be read at once. | Workflows and spend limits are always on. | `ALWAYS_ON` | spine. |
| Profile | A named set of plugins, custom items and instructions, chosen whole. | The research profile turns on delegate and digest-now. | `Profile`, `harness profile` | bundle (when a profile is meant). |
| Setup | Everything the agent is composed of right now: which plugins are on, which profile stands, which skills are mounted. | The setup has twelve plugins on. | `setup` | loadout, composition (when this is meant). |
| Custom item | Something the agent wrote for itself at runtime: a custom capability, a custom step, a custom agent, a custom profile. Lands pending, the owner activates it. | The custom capability waits for activation. | `custom_capability`, `custom_step`, `write_custom_step`, `CustomItemType`, `CustomItems`, `FileCustomCapabilities`, `FileCustomSteps`, `FileCustomAgents`, `FileCustomProfiles` | authored item, authored artifact, author_work_leaf. |
| Model tier | One of three roles a model runs in: orchestrator, worker, helper. The owner sets each tier's model. | The worker tier runs on the flash model. | `Tier`, `TierSetting`, `StepModelWrapper`, `ModelBuilder` | seat. |
| Role | What an agent definition is: its identity, its job, the skills it holds, its tools, model and budget. | The reviewer role holds two skills and no write tool. | `author_agent`, `ProfileDoc` | seat (an agent's whole position). |
| Delegation tier | One of three effort settings a delegate is called with: quick, standard, deep. Each maps onto one model tier. | The delegate ran at the deep tier. | `TierKey` | menu key. |
| Delegate | The one coding agent a session hands a task to, briefed with that task alone. | The delegate fixed the test and reported back. | `delegate_task`, `delegate_agent` | worker (the agent). |
| Run workspace | The folder and command environment one agent run's shell, file tools and skills work through, pydantic-ai's workspace. | The delegate's commands start in its run workspace. | `LocalWorkspace`, `chat_workspace`, `step_workspace`, `ctx.workspace` | unchanged. |
| Rebuild | Recomputing the session projection from the session logs. | The rebuild restored the 69 sessions. | `harness sessions rebuild` | sync (on the sessions side). |
| Note overlay | The owner's notes shown over a project file. | The note overlay marks two open threads. | `NoteOverlay` | margins. |
| Owner note | A comment the owner anchors to a span of a file in a project or a knowledge base, with its thread of replies and its status. | The owner note on the second heading asks the agent to shorten it. | `Note` (engram.kb's note-thread model), `NoteSheet`, `NoteView`, `reply_note` | margin comment. |
| Page target type | What one thing the current page lets the companion touch is: a tab group, an input, or a region. | The page target type of the run tabs is a tab group. | `PageTargetType` (the `kind` field of a declared target stays) | `TargetKind`. |
| Friction target type | What a friction flag is about: a tool, a skill, a capability, a workflow, an agent, the instructions, or the agent itself. | The friction target type of that flag is a skill. | `FrictionTargetType` (the `target_kind` field, tool argument and CLI option stay) | `TargetKind`. |
| Session type | What a session was opened as: chat, runner or master. | The session type of a scheduled fire is runner. | `SessionType` (the `session_kind` column and log field and the `kind` listing field stay) | `SessionKind`. |
| Registry of projects | The append-only list of project folders this harness knows. | The registry of projects lists the checkout and two other folders. | `project_registry`, `registered_projects`, `RegisteredProject`, `ProjectRegistered`, `ProjectUnregistered` | registry (bare). |
| Skill hint | One line naming the skill a decision model picks for a turn's message, shown to the model on the turn's first request and never saved. | The skill hint named designing-workflows. | `SkillHint`, `skill_hint` | unchanged. |
| Permutation self-consistency | Asking one pick-one question in several option orders and averaging each option's probability. | Permutation self-consistency steadied the skill pick. | `DecisionModelQuestions.pick(permutations=...)` | unchanged. |

### 4.7 Words retired everywhere

| Word | Why | What says it instead |
|---|---|---|
| shelf | Means nothing to a reader. | library, reusable step, workflows. |
| block | Three meanings across two contexts. | workflow file, step file, claim, decision block (schema key only). |
| door | Not a term anyone outside knows. | plugin. |
| parked | Unclear. | waiting (at a gate), abandoned (attempts used up). |
| ledger | Five things, and jargon. | source queue, evidence, activations, refresh history, retirement record. |
| journal | Jargon for log. | session log, run log. |
| artifact (bare) | Three things. | workflow, reusable step, custom item, report file. |
| leaf, work, task, brought, carried | Five words for one thing, none standard. | step, inline step. |
| seat | Not a word the owner wants. | model tier. |
| world | Nobody outside knows what a world is. | environment. |
| loadout | Gaming jargon. | setup. |
| authored | Odd. | custom. |
| attestation | Jargon. | evidence. |
| kind | Nobody says kind, everybody says type. | page type, source type, job type. |
| spec (for a knowledge base) | Schema is the word everyone uses for a declared structure. | schema. |
| compile | A programming word for what search engines call indexing. | index. |
| lint | Programming jargon. | validation. |
| intake, medium | Administrative and academic words. | source queue, source type. |
| bundle | A packaging word. Everyone says template. | template. |
| library visibility | Nobody says it. | private, shared. |
| calibration, advisory | Precise but unfamiliar. | tuning run, recommendation. |
| corpus, vault, collection (as the domain word) | The word a person says is knowledge base. | knowledge base, everywhere, code included. |
| provider (ours) | Collides with pydantic-ai's model provider, and a mother would not know what it reads. | connector. |
| dashboard, studio (for the SPA) | Three names for one screen. | AI Factory Studio. |
| wiki, memory (as database names) | Neither says knowledge base. | database `knowledge_bases`, database schema `kb`, graph `kb_graph`. |

### 4.8 Qualified words

These words stay, but never bare: registry (registry of knowledge bases, registry of steps, registry of skills, registry of projects), run
(workflow run, refresh run, evaluation run, tuning run), log (session log, run log, the app's own logging is "logging"),
fingerprint (index fingerprint, workflow fingerprint), tier (model tier, delegation tier), type (page type, source type,
job type), schema (knowledge-base schema, database schema, never for a workflow), validation (note validation, workflow
validation), event (domain event, log entry, change notification), worker (background worker, job runner, the worker
model tier), library (the workflow library only).

The word workspace stays too, never bare: run workspace (pydantic-ai's), workspace folder (`WORKSPACE`, set by `APP_WORKSPACE`, the folder the chat agent's run workspace opens on), and project root (`ACTIVE_WORKSPACE_ROOT`, the project folder of the session whose turn is running).

## 5. Events and commands

### 5.1 What passes today

Every command in the repository is imperative, has one handler, and can be refused: `CompileCorpus`, `IngestPage`,
`ExportOkf`, `SyncMirrors`, `ScanFeeds`, and the nine evals commands. Every bus event is past tense and appended once.
The session log's entry kinds are events by all three tests. The change frames on the coordination channel are
thin events (topic and time, no payload), so they are events too, under the name change notification.

### 5.2 Findings

| # | Finding | Rule broken | Fix |
|---|---|---|---|
| E1 | `PapersChanged` names a research page type inside the general indexer. | The knowledge base's words live in its template. | Rename to `CitingPagesChanged`. |
| E2 | The refresh has no events. Its lifecycle is job states (`queued`, `running`, `done`, `parked`), and the refresh history is projected from a job table. | A lifecycle that matters is facts, and a history is a projection of facts. | Add `RefreshStarted`, `RefreshCompleted`, `RefreshAbandoned` on the bus. Project the refresh history from them. |
| E3 | A workflow run's lifecycle (`RunLaunched`, `RunPaused`, `RunResumed`, `RunStopped`, `RunFinished`, `GateDecided`) is written only into the session log. | Separation of concerns. A run's facts belong to Workflows. The session only launched it. | Workflows records its own run events. The session log keeps `RunLaunched` as the session's fact and nothing else about the run. Ruled by the owner. |
| E4 | The activation records are named as nouns: `capability_activation`, `leaf_activation`. | Events are past tense. | `CapabilityActivated`, `StepActivated`, `AgentActivated`, `ProfileActivated`, `CapabilityPromoted`. |
| E5 | `decide_page` changes a page's status and raises nothing. | A decision is a fact other contexts react to. | Add `PageDecided(knowledge base, page, outcome)`. |
| E6 | The workflow library raises nothing when a workflow is saved or adapted. | Saving is the fact a schedule and the Studio react to. | Add `WorkflowSaved`, `WorkflowAdapted`, `EvidenceRecorded`. |
| E7 | `SessionSwitched` records a screen act, not a fact about the conversation. | The log is the conversation's record. | Removed from the log. Ruled by the owner. |

### 5.3 The target event list, by context

| Context | Commands | Events |
|---|---|---|
| Knowledge base | `IndexKnowledgeBase`, `IngestPage`, `ExportOkf`, `DecidePage` (new) | `IndexingStarted`, `NotesParsed`, `ValidationCompleted`, `PagesLoaded`, `CitingPagesChanged`, `EmbeddingsRefreshed`, `IndexingCompleted`, `PageIngested`, `PageDecided` (new), `ExportStarted`, `ConceptsProjected`, `ExportCompleted` |
| Sources | `SyncMirrors`, `ScanWatchList`, `RequestSource` (today a tool, becomes a command) | `SyncStarted`, `SyncCompleted`, `SourceRequested` (new), `SourceResolved` (new) |
| Registry of knowledge bases | `RefreshKnowledgeBase` (today the job kind) | `RefreshStarted`, `RefreshCompleted`, `RefreshAbandoned` (all new) |
| Evals | the nine today, `CalibrateRetrieval` becomes `TuneRetrieval`, `AdviseStructure` becomes `RecommendStructure` | the twelve today, `Calibration*` become `Tuning*`, `Advisory*` become `Recommendation*` |
| Workflows | `SaveWorkflow`, `AdaptWorkflow`, `LaunchRun`, `DecideGate`, `PauseRun`, `ResumeRun`, `StopRun` | `WorkflowSaved`, `WorkflowAdapted`, `EvidenceRecorded`, `RunLaunched`, `GateReached`, `GateDecided`, `RunPaused`, `RunResumed`, `RunStopped`, `RunFinished` |
| Sessions | none on the bus, the turn is the command | the log entry kinds, with E4 applied and E7 removed |

## 6. Boundary findings

Crossings that are not contracts today. Each is a candidate for its own change.

| # | Crossing | Rule broken | Fix |
|---|---|---|---|
| B1 | The harness reads the registry of knowledge bases straight from engram's settings file, and opens each knowledge base's schema file from its folder. | A context is entered through its contract, not its file. | The studio API serves the registry, names, folders and schemas. Ruled, R16. |
| B2 | Evals writes the retrieval policy file into the knowledge base's folder. | Same. | A command on the Knowledge base: `ApplyRetrievalPolicy`. The knowledge base owns the file. |
| B3 | Sources writes mirror files into the knowledge base's folder, and the indexer reads them. | A file-based crossing with no declared contract. | Declare the mirror file format as the contract, in one place, owned by Sources. |
| B4 | `notify_owner` in the harness counts "pending flags in a collection". | A knowledge base's words live in its template. | It counts pending decisions, through the Knowledge base's decisions read. |
| B5 | The authoring tools live under `engram.retrieval` and perform writes and indexing. | Naming. Retrieval reads. | Move them to `engram.authoring`, the Knowledge base's write contract. |
| B6 | AI Factory Studio renders `run_sql` as "Queried the corpus" whatever it hit. | Surface words follow the vocabulary. | "Queried the knowledge bases". |
| B7 | The workflow fingerprint stamps the orchestrator's model id. | The fingerprint is the workflow's environment, not the orchestrator's. | Stamp the steps' contracts, the tools and the capabilities the workflow needs. Drop the orchestrator model id. Ruled by the owner. |

## 7. The rename map

Ordered by what a reader meets first. Cost: S is a text change, M touches identifiers and tests, L touches data on disk,
a database, or every knowledge base's schema file.

| Today | Target | Where | Cost |
|---|---|---|---|
| corpus, corpora, CorpusSpec, corpus_root, corpusspec.json, `/api/corpora`, `/corpora`, `studio corpora` | knowledge base, knowledge_bases, KnowledgeBaseSchema, knowledge_base_root, knowledge-base-schema.json, `/api/knowledge-bases`, `/knowledge-bases`, `studio knowledge-bases` | every package, every knowledge base's schema file on disk, the SPA routes | L |
| collection (as the domain word in prose, tool text, hit fields) | knowledge base | engram, harness, SPA. The database column keeps its name until the schema rename below. | M |
| database `wiki`, database schema `memory`, graph `wiki_kg` | `knowledge_bases`, `kb`, `kb_graph` | one migration, the settings file, docker compose, every SQL string and docstring naming them | L |
| kind, page_kind, page_types | page type, page_type, page_types | engram, studio, harness, SPA | M |
| spec, SchemaSpec, CorpusSpec, `_meta/schema.md` | schema, KnowledgeBaseSchema | engram, studio, harness | M |
| compile, CompileCorpus, `studio compile`, compile_manifest, compile_fingerprint, incremental compile, full rebuild | index, IndexKnowledgeBase, `studio index`, index_manifest, index_fingerprint, incremental index, full index | engram, studio, SPA, docs, the plugin hook | L |
| lint, LintReport, lint_store, `lint` rule names | validation, ValidationReport, validation_store, rule names unchanged | engram, studio, SPA | M |
| RegistryIndex, `_meta/registry/` | VocabularyIndex, `_meta/vocabulary/` | engram, every knowledge base on disk | L |
| shelf (all uses), store, WorkflowStore, WorkflowCollection | library, WorkflowLibrary | core, harness, SPA, docstrings | M |
| `pack_workflow`, `pack_draft`, `PackRefusedError`, `pack` (the store call) | `save_workflow`, `save_draft`, `SaveRefusedError`, `save` | core, harness, SPA button, skills text | M |
| `harness pack <dir>`, `pack_path` | `harness import <dir>`, `import_path` | harness CLI, tests, docs | M |
| pack (new meaning) | `pack_session`: review this session and draft a workflow or adapt one | a new plugin, not a rename | new work |
| block, `harness/blocks/` | workflow file, step file, `harness/workflows/{steps,workflows}/` | folder, CLI help, tests, docs | M |
| door, `doors.py`, `harness doors`, `DoorSet`, `Switched(door=...)`, `Door` | plugin, `plugins.py`, `harness plugins`, `PluginSet`, `Switched(plugin=...)`, `Plugin` | harness, SPA control room, session logs on disk (a compatibility read for old `door_set` entries) | L |
| `CorpusDoor`, authoring doors | `KnowledgeBaseConnection`, authoring tools | engram, harness | M |
| `HumanGate` | `PersonGate` | core, harness, SPA | M |
| parked (gate), parked (job) | waiting, abandoned | core.cqrs job states, harness run phase, studio refresh states, SPA labels | M |
| ingest gate, faithfulness gate, machine staleness gate, freshness sweep | validation, quote check, freshness check | engram, studio prose | S |
| provider (ours), `SourceProvider`, `engram.sources.providers`, `sources.providers`, package `engram_providers` | connector, `Connector`, `engram.sources.connectors`, `sources.connectors`, package `engram_connectors` | engram, providers, studio, every knowledge base's schema file | L |
| `IntakeKind`, intake, IntakeLedger, `intake.yaml` | `SourceType`, source queue, SourceQueue, `source-queue.yaml` | engram, providers, studio, the file in every knowledge base's `_meta/` | L |
| feed, feeds file, FeedSpec, FeedScanner, `feeds.yaml` | watch list, watch-list file, WatchEntry, Scanner, `watch-list.yaml` | engram, providers, studio, the file in every knowledge base's `_meta/` | L |
| bundle, `bundles/`, `studio init --bundle` | template, `templates/`, `studio init --template` | repo layout, studio, SPA, docs, the import-boundary tests | M |
| `library: false`, `retrieval.library` | `private: true` | settings file, every knowledge base's schema file, engram, harness | L |
| registry_targets, corpora (settings key) | registered_knowledge_bases, knowledge_bases | harness, studio, the settings file | M |
| job kind `corpus.ingest`, `studio ingest`, `RunRecord` (studio), run ledger, corpus worker | `kb.refresh`, `studio refresh`, `RefreshRun`, refresh history, background worker | studio, SPA, jobs table rows in flight | M |
| calibration, CalibrateRetrieval, advisory, StructuralAdvisory, AdviseStructure | tuning run, TuneRetrieval, recommendation, Recommendation, RecommendStructure | engram, docs | M |
| `WorkRegistry`, `WorkflowSpec`, `TaskSpec`, `CodeSpec`, `WorkerSpec`, `BroughtStep`, `tasks` (YAML key) | `StepRegistry`, `Workflow`, `Briefing`, `Program`, `AgentStep`, `InlineStep`, `inline` | core, harness, saved workflows on disk and in the library | L |
| `build_world`, `RunFingerprint`, `RunFingerprint.world` (orchestrator model id inside) | `build_environment`, `WorkflowFingerprint`, `.environment` without the orchestrator's model | core, harness, saved fingerprints (a re-stamp on next save) | L |
| attestation, AttestationLedger, trust tier, TrustRecord | evidence, RunEvidence, trust level, TrustLevel | core, harness, SPA, the evidence file on disk | L |
| session journal, `session-journal.jsonl`, journal event, run journal, RunJournal | session log, `session-log.jsonl`, log entry, run log, RunLog | harness, docs, the file name on disk | L |
| step log, step store (prose), model record | transcript | harness docs | S |
| loadout, authored artifact, author_work_leaf, `author_*` tools | setup, custom item, write_custom_step, `write_custom_*` | harness, SPA control room, skills text | M |
| seat (nowhere yet), `TierKey` | model tier, delegation tier | prose only | S |
| `registry_session` | `library_composition` | harness | S |
| `harness sessions sync` | `harness sessions rebuild` | harness CLI, docs | S |
| `PapersChanged` | `CitingPagesChanged` | engram, studio | M |
| `capability_activation` and four siblings | `CapabilityActivated` and siblings | harness, activation files on disk | L |
| `SessionSwitched` | removed | harness, SPA | M |
| change event (prose) | change notification | core, harness, studio, SPA docs | S |
| dashboard, studio (for the SPA) | AI Factory Studio | docs | S |
| drafting mirror, eval run | drafting text, evaluation run | engram | S |
| `Repository`, `Store` (our own write ports) | the plural of what the port holds: `Pages`, `Schedules`, `EvaluationRuns` | core, engram, harness | M |
| `Reader`, `Reads`, `Repository` (read ports) | `<Thing>Queries`: `SeedQueries`, `InspectionQueries` | core, engram, harness, studio | M |
| ledger (spend, job, evidence, budget), journal, attest | spend log, recent jobs, evidence, budget record, trial log, record evidence | core, harness, engram | M |
| spec (a knowledge base or its parts), `PageTypeSpec` and siblings, spec (a workflow), `fetch_spec`, `compile_spec` | schema, `PageTypeSchema` and siblings, workflow, `fetch_workflow`, `build_graph` | core, engram, studio, harness | M |
| kind (as a type), `TargetKind`, `HarnessKind` and siblings | page type, finding type, item type, target type, `PageTargetType`, `CustomItemType` (on-disk and wire `kind` fields stay) | core, engram, studio, harness | M |
| work (a step), line (a step's conversation), `WorkerLine`, `TurnOnALine` | step, step conversation, `StepConversation`, `StepConversationTurn` | core, harness | M |
| `StepShelf` | `ReusableStepQueries` | harness | S |
| door (lazy imports, a connection), `_DOORS` | lazy imports, connection, `_LAZY_IMPORTS` | engram | S |
| `Eval*` identifiers, golden dataset, `GoldenDataset`, `GoldenCase` | `Evaluation*`, evaluation dataset, `EvaluationDataset`, `EvaluationCase` | engram | M |
| once, tick_once (a background loop's pass) | tick | core, harness | S |
| money brake, `MoneyBrake` | spend limit, `SpendLimit` | harness | S |
| margins, `Margins` | note overlay, `NoteOverlay` | harness | S |
| verb-named ports and capabilities (`BuildModel`, `CallTool`, `EmbedQuery`, `DiscloseResults`) | doer and action nouns (`ModelBuilder`, `ToolCaller`, `QueryEmbedder`, `ResultDisclosure`) | core, engram, harness | M |
| `SupersessionIncomplete`, `NoteRefused`, `NoKnowledgeBase` and other errors | `<Thing><Problem>Error`: `SupersessionIncompleteWarning`, `NoteRefusedError`, `KnowledgeBaseMissingError` | core, engram, studio, connectors | S |

## 8. Keeping it so

1. This document is the vocabulary. The four `CONTEXT.md` glossaries become one section each of it, or one line each
   pointing here. A term is added here, with its sentence, before it is used anywhere.
2. A lint fails a commit whose docs, docstrings, UI copy or CLI help contain a
   retired word from 4.7, a bare word from 4.8, or a banned symbol (em dash, en dash, semicolon, middle dot, arrow).
3. Every event and command added later passes the three tests before it gets a name, and the name goes into 5.3.
4. Every boundary crossing added later is a contract: a module other packages import, an HTTP route, or an event.

## 9. Rulings

The owner's rulings, in the owner's own terms, and what each one settled above.

| # | Ruling | Settles |
|---|---|---|
| R1 | Pack means distilling a session into a workflow, like compacting a conversation: review what happened, draft a new workflow when it is worth keeping, or adapt an existing one. It never means storing. | Pack, Save, Import in 4.5. The port verb and the CLI verb are renamed, and pack becomes new work. |
| R2 | Shelf means nothing. | Retired, 4.7. |
| R3 | The word is knowledge base, everywhere, code included. Speak like a human. No leftovers, no half job. | 4.1, the first rows of the rename map, cost L accepted. |
| R4 | Door is not a term anyone knows. Plugin. | 4.6, the rename map. |
| R5 | A gate is the check of a step's outcome inside a workflow, decided by a person, the orchestrator, or a judge step. Nothing else is a gate. The other three checks are mechanical checks and get their own names. | 4.5 Gate, 4.1 Validation, 4.4 Quote check and Freshness check. |
| R6 | Tier stays for both the model tiers and the delegation tiers. Seat is not a word the owner wants. | 4.6. Trust tier becomes trust level so tier keeps two qualified meanings, not three. |
| R7 | The fingerprint stamps the workflow's environment: the steps, tools and capabilities the workflow needs. Never the orchestrator's. | 4.5 Environment and Fingerprint, finding B7. |
| R8 | A registry is a registry of something. Always say of what. | 4.8. The bare "registry" of the studio glossary becomes the registry of knowledge bases. |
| R9 | Run stays one word across workflow runs, refresh runs, evaluation runs and tuning runs. | 4.8. |
| R10 | No wiki, no memory. The names say knowledge base. | 4.7, the database and schema rename. |
| R11 | A run's lifecycle belongs to Workflows, not to the session log. Separation of concerns. | E3. |
| R12 | A session switch is not a fact the log cares about. | E7, removed. |
| R13 | The product surface is AI Factory Studio. | 4.7. |
| R14 | Parked is not clear. | Waiting at a gate, abandoned after the attempts. |
| R15 | A stranger must understand every term at once, from a sentence that uses it. | Rule 5 in section 2, the sentence column in every table. |
| R16 | Boundaries, not a common contract. The registry of knowledge bases belongs to the studio, and the harness reads it over the studio's HTTP API, names, folders and schemas alike. No module both apps import to read one file. | B1, and Task 6 of the every-corpus plan. |
| R17 | A person names a source or a watch-list entry ("tell engram which X account to watch"), never the code that reads a host. That code is a connector, the standard name in data tools. Adapter and reader lose: adapter fails the stranger test, reader already names the read-only database role. | 4.2, the rename map. |
| R18 | Standard names first: wherever most people already agree on a word, that word wins over one we invented. | Rule 7 in section 2, and these swaps: kind to type, spec to schema, compile to index, lint to validation, intake to source queue, medium to source type, bundle to template, library visibility to private, calibration to tuning run, advisory to recommendation, store to library, stored step to reusable step, carried step to inline step, journal to log, model record to transcript, world to environment, loadout to setup, authored to custom, attestation to evidence, corpus worker to background worker. The owner may veto any one of them. |

## 10. Open questions

None. Every word has a ruling, and the owner accepted the whole document on 2026-09-03.
