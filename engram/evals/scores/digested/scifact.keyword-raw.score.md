# Search score: scifact.yaml on scifact

300 case(s), scored per document with search alone at k = 50 under the keyword-raw search setup.

| Metric | Mean |
|---|---|
| ndcg@10 | 0.6709 |
| recall@1 | 0.5275 |
| recall@5 | 0.7287 |
| recall@10 | 0.7936 |
| mrr | 0.6427 |

Hits that named no source document: 0.
Gold ids that name a page with no source document: 0.
Cases left out because no gold id names a source document: 0.
Recall past 10 is not reported, because 50 chunk hits can collapse to fewer than 50 documents.

Cases that ranked no gold document (33): 1, 13, 132, 421, 431, 437, 452, 475, 502, 535, 560, 619, 716, 775, 870, 913, 914, 975, 1049, 1088, 1110, 1132, 1175, 1191, 1199, 1200, 1213, 1221, 1226, 1280, 1281, 1332, 1395
