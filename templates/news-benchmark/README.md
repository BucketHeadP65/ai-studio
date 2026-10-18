# News benchmark

A knowledge base that holds the MultiHop-RAG benchmark's news articles filter-first, so an agent can answer a question
that spans several articles with SQL filters, search and the graph, and the dataset's questions can score it. Each
article becomes one source mirror under `Sources/`. Its digest carries the article's outlet, author, publication time,
publication date, category and url as fields, copied from the source metadata with no model call. Its search card prints
the outlet, the category and the publication date. A search's `since` and `until` read that publication date, because the
schema's `retrieval.date_field` names `published_on`, never the day the digest was written. A page without that field,
such as an entity page, falls outside every date range, so a dated search returns digests and their raw chunks only.
Each claim carries a quote, the date it is about and any figure it reports, and links its subject and speaker to entity
pages. Entity resolution gives every person, organization, product, event, place and work one entity page, and a relate
pass links each digest to the earlier digests on the same story.

## Dataset

| Measure | Value |
|---|---|
| Articles | 609, published 2023-09-26 to 2023-12-25 |
| Outlets | 49, seeded verbatim as the `outlet` facet |
| Categories | business, entertainment, health, science, sports, technology, seeded as the `category` facet |
| Questions | 2,556, by query type: comparison 856, inference 816, temporal 583, null 301 |
| License | ODC-BY 1.0 |

MultiHop-RAG is the work of Yixuan Tang and Yi Yang, "MultiHop-RAG: Benchmarking Retrieval-Augmented Generation for
Multi-Hop Queries", COLM 2024. The data is the Hugging Face dataset
[yixuantt/MultiHopRAG](https://huggingface.co/datasets/yixuantt/MultiHopRAG), used under the Open Data Commons
Attribution License (ODC-BY 1.0), which requires this attribution wherever the data or a database built from it is
shared.

The `multihop_rag` connector downloads the dataset's two files into `_meta/multihop-rag-cache/` at one pinned revision,
and writes the questions there as `judgments.jsonl`. That folder is never committed. When the knowledge base folder is
a git repository, list `_meta/multihop-rag-cache/` in its `.gitignore`.

## Steps

1. Start the knowledge base under the dataset's name:
   `studio init --template news-benchmark --slug multihop-rag --knowledge-base <folder>`.
2. Register it, then run `studio sync` and `studio index`. The first sync downloads the dataset and mirrors all 609
   articles, each with its outlet, author, publication time, publication date, category and url in the source
   metadata.
3. Turn the questions into an evals dataset:
   `studio evals import-judgments --file _meta/multihop-rag-cache/judgments.jsonl --out <dataset>.yaml`. Add
   `--per-type 100 --salt <salt>` for a sample of 100 questions per query type. The same salt keeps the same sample.
4. Digest the articles. The digest workflow only writes digests, and the link workflow in step 5 links their entities.
   From `harness/`, import the shared steps, then this template's workflows:

   ```bash
   uv run harness import workflows
   uv run harness import ../templates/news-benchmark/workflows
   ```

   Then start the `news-digest` workflow with `POST /api/run` on the harness. Its parameters go under `arguments`.
   `picked_sources` names the articles to digest by source id. Left empty, the run takes the batch of 50 the base's
   digest-sources policy entry admits. An id that names no undigested article is left out with no error.

   ```json
   {"workflow_id": "wf.news-digest.v2", "arguments": {"collection": "multihop-rag", "picked_sources": "<ids, optional>"}}
   ```

5. Link the digests' entities after every digest run has ended. The pass lists every active digest with no `mentions`
   link, reads each one from its file as a ready payload, and writes it back with its entity links:

   ```json
   {"workflow_id": "wf.news-link.v1", "arguments": {"collection": "multihop-rag"}}
   ```

6. Relate the digests once every article is digested and the link run has ended, so the index holds every digest and
   its links:

   ```json
   {"workflow_id": "wf.news-relate.v2", "arguments": {"collection": "multihop-rag"}}
   ```
