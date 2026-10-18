# Search score: scifact.yaml on scifact

300 case(s), scored per document with search alone at k = 50 under the hybrid-card search setup.

| Metric | Mean |
|---|---|
| ndcg@10 | 0.6798 |
| recall@1 | 0.5159 |
| recall@5 | 0.7508 |
| recall@10 | 0.8209 |
| mrr | 0.6468 |

Hits that named no source document: 0.
Gold ids that name a page with no source document: 0.
Cases left out because no gold id names a source document: 0.
Recall past 10 is not reported, because 50 chunk hits can collapse to fewer than 50 documents.

Cases that ranked no gold document (17): 1, 128, 421, 437, 502, 690, 716, 834, 887, 1024, 1110, 1191, 1199, 1213, 1332, 1362, 1363
