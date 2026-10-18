# Paired comparison

A case is correct when its answer matches the expected answer strictly, and a failed case counts as incorrect. The difference is the candidate's strict accuracy minus the baseline's, in points, with a 95 percent interval: the mean per-case difference plus or minus 1.96 standard errors. Baseline only and candidate only count the cases only that run answered correctly, which McNemar's exact two-sided test reads. Holm's method adjusts the p values across the 1 pair(s) of this comparison, over all cases and within each query type. The other knowledge base columns count the cases whose SQL rows from a view of knowledge base content named another knowledge base, and the failed columns count the cases that raised instead of answering. The retrieval table gives each run's mean source-level recall@4, recall@10 and MRR over the cases it scored: those whose citations it mapped to sources and that have gold source ids, a failed case scoring 0. None means the run scored no case there.

## b.jsonl against a0.jsonl

Dataset engram-retrieval-golden (6c6df7335779), model gpt-6-luna, 400 cases. Baseline: strategy search-hybrid, SQL off, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag. Candidate: strategy search-hybrid, SQL on, code mode off, thinking xhigh, knowledge base 990584ffc2c64f63c934b452ffc7bef58644ef22be9dc0bdd7897e2c2c1abe81:multihop-rag.

| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |
|---|---|---|---|---|---|---|---|---|
| all cases | 400 | 77.5% | 82.0% | +4.5 (+1.1 to +7.9) | 16 | 34 | 0.0153 | 0.0153 |
| comparison_query | 100 | 63.0% | 75.0% | +12.0 (+2.6 to +21.4) | 6 | 18 | 0.0227 | 0.0227 |
| inference_query | 100 | 97.0% | 97.0% | +0.0 (-2.8 to +2.8) | 1 | 1 | 1.0000 | 1.0000 |
| null_query | 100 | 92.0% | 93.0% | +1.0 (-4.9 to +6.9) | 4 | 5 | 1.0000 | 1.0000 |
| temporal_query | 100 | 58.0% | 63.0% | +5.0 (-2.6 to +12.6) | 5 | 10 | 0.3018 | 0.3018 |

| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed | Baseline other knowledge base | Candidate other knowledge base |
|---|---|---|---|---|---|---|
| all cases | 77.5% | 81.8% | 6 | 9 | 0 | 0 |
| comparison_query | 62.0% | 73.0% | 1 | 3 | 0 | 0 |
| inference_query | 98.0% | 98.0% | 0 | 0 | 0 | 0 |
| null_query | 92.0% | 93.0% | 5 | 1 | 0 | 0 |
| temporal_query | 58.0% | 63.0% | 0 | 5 | 0 | 0 |

| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 | Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |
|---|---|---|---|---|---|---|---|---|
| all cases | 300 | 300 | 0.604 | 0.710 | 0.604 | 0.710 | 0.853 | 0.905 |
| comparison_query | 100 | 100 | 0.665 | 0.795 | 0.665 | 0.795 | 0.833 | 0.908 |
| inference_query | 100 | 100 | 0.495 | 0.608 | 0.495 | 0.608 | 0.857 | 0.897 |
| null_query | 0 | 0 | none | none | none | none | none | none |
| temporal_query | 100 | 100 | 0.653 | 0.728 | 0.653 | 0.728 | 0.870 | 0.910 |
