# Search score: scifact.yaml on scifact

300 case(s), scored per document with search alone at k = 50 under the hybrid-raw search setup.

| Metric | Mean |
|---|---|
| ndcg@10 | 0.7474 |
| recall@1 | 0.6141 |
| recall@5 | 0.7979 |
| recall@10 | 0.8627 |
| mrr | 0.7243 |

Hits that named no source document: 0.
Gold ids that name a page with no source document: 0.
Cases left out because no gold id names a source document: 0.
Recall past 10 is not reported, because 50 chunk hits can collapse to fewer than 50 documents.

Cases that ranked no gold document (11): 1, 421, 437, 502, 560, 716, 975, 1110, 1199, 1213, 1332
