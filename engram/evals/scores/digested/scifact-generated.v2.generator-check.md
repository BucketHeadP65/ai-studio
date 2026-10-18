# Generator check: scifact-generated.v2.yaml against scifact.yaml on scifact

The check failed: the two datasets rank the search setups in different orders.

| Search setup | Human nDCG@10 | Human rank | Generated nDCG@10 | Generated rank |
|---|---|---|---|---|
| hybrid | 0.7568 | 2 | 0.9437 | 2 |
| keyword | 0.6740 | 6 | 0.9378 | 3 |
| hybrid-raw | 0.7474 | 3 | 0.9031 | 6 |
| keyword-raw | 0.6709 | 7 | 0.9253 | 5 |
| hybrid-card | 0.6798 | 5 | 0.8505 | 7 |
| hybrid-section | 0.7579 | 1 | 0.9299 | 4 |
| hybrid-claim | 0.7237 | 4 | 0.9450 | 1 |

Kendall's tau-b: 0.1429.
Setup pairs both datasets order the same way: 12 of 21.
Each dataset ranks the setups by its own nDCG@10 rounded to 4 decimals, highest first, and setups with the same rounded nDCG@10 share a rank.
