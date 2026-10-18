# Benchmark dataset

A knowledge base that holds one benchmark dataset from the BEIR collection, so its search can be scored against
the dataset's human relevance judgments with `studio evals score`. Each document becomes one source mirror under
`Sources/`, and the indexer chunks it into raw chunks. The `digest` page type is there for the digest step, which
can later turn documents into pages.

## Datasets

| Dataset | Documents | Test queries | Judgments | License |
|---|---|---|---|---|
| `scifact` | 5,183 | 300 | binary | CC BY-NC 2.0 |
| `nfcorpus` | 3,633 | 323 | graded (0, 1, 2) | research use, see the NFCorpus terms |

The `beir` connector downloads the dataset's archive from the BEIR authors into `_meta/beir-cache/`, which is never
committed. Each dataset keeps its own license.

## Steps

1. Start the knowledge base under the dataset's name, so two knowledge bases from this template never share a
   collection: `studio init --template benchmark-dataset --slug scifact --knowledge-base <folder>`.
2. Write `_meta/beir.yaml` with two lines, `dataset: scifact` and `split: test`.
3. Register it, then run `studio sync` and `studio index`. The first sync downloads the dataset.
4. Digest the documents. This step is optional. From `harness/`, import the shared steps, then this template's
   workflow:

   ```bash
   uv run harness import workflows
   uv run harness import ../templates/benchmark-dataset/workflows
   ```

   Then start the `benchmark-digest` workflow with `POST /api/run` on the harness. Its parameters go under
   `arguments`. `picked_sources` is optional and names the documents to digest by source id. An id that names no
   undigested document is left out with no error.

   ```json
   {"workflow_id": "wf.benchmark-digest.v3", "arguments": {"collection": "<name>", "picked_sources": "<ids, optional>"}}
   ```

   Each digest carries quoted claims. `studio evals score` maps a digest hit to the document it digests, so scoring
   works with or without digests.
5. Turn the judgments into an evals dataset:
   `studio evals import-judgments --file _meta/beir-cache/scifact/judgments.test.jsonl --out <dataset>.yaml`.
6. Score search: `studio evals score --dataset <dataset>.yaml`.
