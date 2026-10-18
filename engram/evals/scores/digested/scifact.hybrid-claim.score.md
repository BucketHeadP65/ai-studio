# Search score: scifact.yaml on scifact

300 case(s), scored per document with search alone at k = 50 under the hybrid-claim search setup.

| Metric | Mean |
|---|---|
| ndcg@10 | 0.7237 |
| recall@1 | 0.5776 |
| recall@5 | 0.7949 |
| recall@10 | 0.8450 |
| mrr | 0.6936 |

Hits that named no source document: 0.
Gold ids that name a page with no source document: 0.
Cases left out because no gold id names a source document: 0.
Recall past 10 is not reported, because 50 chunk hits can collapse to fewer than 50 documents.

Cases that ranked no gold document (21): 1, 13, 124, 232, 294, 303, 312, 421, 437, 502, 508, 560, 715, 716, 887, 1049, 1110, 1199, 1213, 1316, 1332
