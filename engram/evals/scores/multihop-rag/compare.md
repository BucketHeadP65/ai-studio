# Paired comparison

A case is correct when its answer matches the expected answer strictly, and a failed case counts as incorrect. The difference is the candidate's strict accuracy minus the baseline's, in points, with a 95 percent interval: the mean per-case difference plus or minus 1.96 standard errors. Baseline only and candidate only count the cases only that run answered correctly, which McNemar's exact two-sided test reads. Holm's method adjusts the p values across the 4 pair(s) of this comparison, over all cases and within each query type. The other knowledge base columns count the cases whose SQL rows from a view of knowledge base content named another knowledge base, and the failed columns count the cases that raised instead of answering. The retrieval table gives each run's mean source-level recall@4, recall@10 and MRR over the cases it scored: those whose citations it mapped to sources and that have gold source ids, a failed case scoring 0. None means the run scored no case there.

## a.jsonl against a0.jsonl

Dataset engram-retrieval-golden (6c6df7335779), model gpt-6-luna, 400 cases. Baseline: strategy search-hybrid, SQL off, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag. Candidate: strategy search-hybrid, SQL off, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag.

| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |
|---|---|---|---|---|---|---|---|---|
| all cases | 400 | 77.5% | 80.8% | +3.2 (+0.1 to +6.4) | 14 | 27 | 0.0596 | 0.2383 |
| comparison_query | 100 | 63.0% | 73.0% | +10.0 (+1.4 to +18.6) | 5 | 15 | 0.0414 | 0.1656 |
| inference_query | 100 | 97.0% | 98.0% | +1.0 (-2.4 to +4.4) | 1 | 2 | 1.0000 | 1.0000 |
| null_query | 100 | 92.0% | 92.0% | +0.0 (-5.6 to +5.6) | 4 | 4 | 1.0000 | 1.0000 |
| temporal_query | 100 | 58.0% | 60.0% | +2.0 (-4.2 to +8.2) | 4 | 6 | 0.7539 | 1.0000 |

| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed | Baseline other knowledge base | Candidate other knowledge base |
|---|---|---|---|---|---|---|
| all cases | 77.5% | 79.2% | 6 | 8 | 0 | 0 |
| comparison_query | 62.0% | 68.0% | 1 | 3 | 0 | 0 |
| inference_query | 98.0% | 98.0% | 0 | 0 | 0 | 0 |
| null_query | 92.0% | 92.0% | 5 | 3 | 0 | 0 |
| temporal_query | 58.0% | 59.0% | 0 | 2 | 0 | 0 |

| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 | Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |
|---|---|---|---|---|---|---|---|---|
| all cases | 300 | 300 | 0.604 | 0.708 | 0.604 | 0.708 | 0.853 | 0.911 |
| comparison_query | 100 | 100 | 0.665 | 0.780 | 0.665 | 0.780 | 0.833 | 0.913 |
| inference_query | 100 | 100 | 0.495 | 0.608 | 0.495 | 0.608 | 0.857 | 0.905 |
| null_query | 0 | 0 | none | none | none | none | none | none |
| temporal_query | 100 | 100 | 0.653 | 0.737 | 0.653 | 0.737 | 0.870 | 0.915 |

## b.jsonl against a.jsonl

Dataset engram-retrieval-golden (6c6df7335779), model gpt-6-luna, 400 cases. Baseline: strategy search-hybrid, SQL off, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag. Candidate: strategy search-hybrid, SQL on, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag.

| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |
|---|---|---|---|---|---|---|---|---|
| all cases | 400 | 80.8% | 82.0% | +1.2 (-1.6 to +4.1) | 15 | 20 | 0.4996 | 0.5364 |
| comparison_query | 100 | 73.0% | 75.0% | +2.0 (-5.4 to +9.4) | 6 | 8 | 0.7905 | 1.0000 |
| inference_query | 100 | 98.0% | 97.0% | -1.0 (-3.0 to +1.0) | 1 | 0 | 1.0000 | 1.0000 |
| null_query | 100 | 92.0% | 93.0% | +1.0 (-4.9 to +6.9) | 4 | 5 | 1.0000 | 1.0000 |
| temporal_query | 100 | 60.0% | 63.0% | +3.0 (-3.5 to +9.5) | 4 | 7 | 0.5488 | 1.0000 |

| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed | Baseline other knowledge base | Candidate other knowledge base |
|---|---|---|---|---|---|---|
| all cases | 79.2% | 81.8% | 8 | 9 | 0 | 0 |
| comparison_query | 68.0% | 73.0% | 3 | 3 | 0 | 0 |
| inference_query | 98.0% | 98.0% | 0 | 0 | 0 | 0 |
| null_query | 92.0% | 93.0% | 3 | 1 | 0 | 0 |
| temporal_query | 59.0% | 63.0% | 2 | 5 | 0 | 0 |

| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 | Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |
|---|---|---|---|---|---|---|---|---|
| all cases | 300 | 300 | 0.708 | 0.710 | 0.708 | 0.710 | 0.911 | 0.905 |
| comparison_query | 100 | 100 | 0.780 | 0.795 | 0.780 | 0.795 | 0.913 | 0.908 |
| inference_query | 100 | 100 | 0.608 | 0.608 | 0.608 | 0.608 | 0.905 | 0.897 |
| null_query | 0 | 0 | none | none | none | none | none | none |
| temporal_query | 100 | 100 | 0.737 | 0.728 | 0.737 | 0.728 | 0.915 | 0.910 |

## c.jsonl against b.jsonl

Dataset engram-retrieval-golden (6c6df7335779), model gpt-6-luna, 400 cases. Baseline: strategy search-hybrid, SQL on, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag. Candidate: strategy full, SQL on, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag.

| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |
|---|---|---|---|---|---|---|---|---|
| all cases | 400 | 82.0% | 80.0% | -2.0 (-5.1 to +1.1) | 24 | 16 | 0.2682 | 0.5364 |
| comparison_query | 100 | 75.0% | 74.0% | -1.0 (-9.1 to +7.1) | 9 | 8 | 1.0000 | 1.0000 |
| inference_query | 100 | 97.0% | 95.0% | -2.0 (-4.8 to +0.8) | 2 | 0 | 0.5000 | 1.0000 |
| null_query | 100 | 93.0% | 88.0% | -5.0 (-11.5 to +1.5) | 8 | 3 | 0.2266 | 0.6797 |
| temporal_query | 100 | 63.0% | 63.0% | +0.0 (-6.2 to +6.2) | 5 | 5 | 1.0000 | 1.0000 |

| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed | Baseline other knowledge base | Candidate other knowledge base |
|---|---|---|---|---|---|---|
| all cases | 81.8% | 79.5% | 9 | 20 | 0 | 0 |
| comparison_query | 73.0% | 72.0% | 3 | 3 | 0 | 0 |
| inference_query | 98.0% | 96.0% | 0 | 1 | 0 | 0 |
| null_query | 93.0% | 88.0% | 1 | 7 | 0 | 0 |
| temporal_query | 63.0% | 62.0% | 5 | 9 | 0 | 0 |

| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 | Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |
|---|---|---|---|---|---|---|---|---|
| all cases | 300 | 300 | 0.710 | 0.700 | 0.710 | 0.700 | 0.905 | 0.893 |
| comparison_query | 100 | 100 | 0.795 | 0.780 | 0.795 | 0.780 | 0.908 | 0.918 |
| inference_query | 100 | 100 | 0.608 | 0.604 | 0.608 | 0.604 | 0.897 | 0.887 |
| null_query | 0 | 0 | none | none | none | none | none | none |
| temporal_query | 100 | 100 | 0.728 | 0.715 | 0.728 | 0.715 | 0.910 | 0.875 |

## d.jsonl against c.jsonl

Dataset engram-retrieval-golden (6c6df7335779), model gpt-6-luna, 400 cases. Baseline: strategy full, SQL on, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag. Candidate: strategy full, SQL on, code mode on, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag.

| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |
|---|---|---|---|---|---|---|---|---|
| all cases | 400 | 80.0% | 82.5% | +2.5 (-0.6 to +5.6) | 15 | 25 | 0.1539 | 0.4616 |
| comparison_query | 100 | 74.0% | 75.0% | +1.0 (-6.6 to +8.6) | 7 | 8 | 1.0000 | 1.0000 |
| inference_query | 100 | 95.0% | 98.0% | +3.0 (-0.4 to +6.4) | 0 | 3 | 0.2500 | 1.0000 |
| null_query | 100 | 88.0% | 95.0% | +7.0 (+1.3 to +12.7) | 1 | 8 | 0.0391 | 0.1562 |
| temporal_query | 100 | 63.0% | 62.0% | -1.0 (-8.1 to +6.1) | 7 | 6 | 1.0000 | 1.0000 |

| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed | Baseline other knowledge base | Candidate other knowledge base |
|---|---|---|---|---|---|---|
| all cases | 79.5% | 82.2% | 20 | 5 | 0 | 0 |
| comparison_query | 72.0% | 74.0% | 3 | 1 | 0 | 0 |
| inference_query | 96.0% | 98.0% | 1 | 0 | 0 | 0 |
| null_query | 88.0% | 95.0% | 7 | 1 | 0 | 0 |
| temporal_query | 62.0% | 62.0% | 9 | 3 | 0 | 0 |

| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 | Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |
|---|---|---|---|---|---|---|---|---|
| all cases | 300 | 300 | 0.700 | 0.730 | 0.700 | 0.730 | 0.893 | 0.927 |
| comparison_query | 100 | 100 | 0.780 | 0.812 | 0.780 | 0.812 | 0.918 | 0.933 |
| inference_query | 100 | 100 | 0.604 | 0.607 | 0.604 | 0.607 | 0.887 | 0.903 |
| null_query | 0 | 0 | none | none | none | none | none | none |
| temporal_query | 100 | 100 | 0.715 | 0.772 | 0.715 | 0.772 | 0.875 | 0.945 |
