# Experiment results

Each cell is mean (min-max) over the runs; `unavailable` means the value was not observable, not zero. With this few runs, treat differences as indicative, not statistically established.

| Measure | diy | managed |
|---|---|---|
| Runs / completed | 5 / 5 | 5 / 5 |
| Required artifacts complete | 5 of 5 | 4 of 5 |
| analysis.py runs on the CSV | 5 of 5 | 5 of 5 |
| Injected anomalies found (recall) | 0.88 (0.80-0.90) | 0.93 (0.90-1.00) [1 missing] |
| Findings reported | 13.0 (12.0-14.0) | 13.6 (13.0-14.0) |
| Findings matching nothing in the key | 1.2 (0.0-2.0) | 1.0 (0.0-3.0) [1 missing] |
| Legitimate patterns flagged as problems | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) [1 missing] |
| Numeric claims supported by recomputation | 7.4 (2.0-11.0) | 5.5 (2.0-8.0) [1 missing] |
| Numeric claims unsupported (not necessarily wrong) | 1.6 (0.0-4.0) | 1.8 (0.0-3.0) [1 missing] |
| Numeric claims unverifiable | 42.2 (30.0-52.0) | 65.0 (55.0-79.0) [1 missing] |
| Active time (s) | 184 (168-207) | 268 (201-325) |
| Model calls | 17.4 (15.0-20.0) | 20.8 (13.0-26.0) |
| Tool calls | 17.8 (15.0-21.0) | 24.0 (14.0-29.0) |
| Input tokens, uncached | 37 (32-42) | 44 (28-54) |
| Output tokens | 22630 (19935-26120) | 33791 (25318-41372) |
| Cache read tokens | 366703 (260331-525734) | 740610 (302790-1037472) |
| Cache write tokens | 44745 (38729-54816) | 66264 (50948-82641) |
| Token cost (USD) | 0.375 (0.331-0.451) | 0.578 (0.411-0.724) |
| Platform runtime charge (USD) | unavailable | 0.0060 (0.0045-0.0072) |
| Total cost (USD) | 0.375 (0.331-0.451) | 0.584 (0.415-0.731) |
| Infrastructure errors recorded | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) |

## Anomalies detected (runs that found each one)

| Entry | diy | managed |
|---|---|---|
| A01 | 4 of 5 | 4 of 5 |
| A02 | 5 of 5 | 4 of 5 |
| A03 | 5 of 5 | 4 of 5 |
| A04 | 5 of 5 | 4 of 5 |
| A05 | 5 of 5 | 4 of 5 |
| A06 | 5 of 5 | 4 of 5 |
| A07 | 5 of 5 | 4 of 5 |
| A08 | 4 of 5 | 4 of 5 |
| A09 | 1 of 5 | 1 of 5 |
| A10 | 5 of 5 | 4 of 5 |

## Per run

| Implementation | Run | Status | Recall | Unmatched | Tool calls | Active s | Cost USD |
|---|---|---|---|---|---|---|---|
| diy | rep-1 | completed | 90% | 0 | 21 | 207 | 0.4509 |
| diy | rep-2 | completed | 90% | 1 | 17 | 168 | 0.3308 |
| diy | rep-3 | completed | 80% | 2 | 18 | 168 | 0.335 |
| diy | rep-4 | completed | 90% | 2 | 15 | 171 | 0.3352 |
| diy | rep-5 | completed | 90% | 1 | 18 | 204 | 0.4226 |
| managed | rep-1 | completed | n/a | n/a | 24 | 235 | 0.5149 |
| managed | rep-2 | completed | 90% | 0 | 25 | 271 | 0.5648 |
| managed | rep-3 | completed | 90% | 3 | 14 | 201 | 0.4154 |
| managed | rep-4 | completed | 90% | 1 | 29 | 310 | 0.6919 |
| managed | rep-5 | completed | 100% | 0 | 28 | 325 | 0.7314 |
