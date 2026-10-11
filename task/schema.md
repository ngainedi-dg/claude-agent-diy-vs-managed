# Dataset and Output Schema

`task/generate_data.py --seed 42` produces `task/data/sales_transactions.csv` (about 10,000 rows, calendar year 2025) and, separately, `evaluation/data/ground_truth.json`.

Only the CSV is ever given to an agent. The ground truth is never mounted, uploaded or referenced in a prompt.

## Columns

| Column | Type | Notes |
|---|---|---|
| transaction_id | string | `T000001`...; duplicated on purpose in a few rows |
| date | string | `YYYY-MM-DD` |
| customer_id | string | `C0001`...`C0900` |
| product_id | string | `P001`...`P041` |
| category | string | Electronics, Furniture, Office Supplies, Apparel, Home & Garden |
| region | string | North, South, East, West, Central |
| quantity | int | Units sold |
| unit_price | float | Price per unit |
| discount_pct | int | 0-60 |
| revenue | float | Recorded revenue, normally `quantity * unit_price * (1 - discount_pct/100)` |

## Seeded entries

| ID | Kind | What was injected |
|---|---|---|
| A01 | injected_anomaly | West region sales decline, 1 Aug to 15 Oct |
| A02 | injected_anomaly | 40-60% discounts on products P003 and P012, 15 Mar to 14 Apr |
| A03 | injected_anomaly | Three-day transaction volume spike, 14-16 Jun |
| A04 | injected_defect | 25 rows where revenue contradicts quantity, price and discount |
| A05 | injected_anomaly | 6 implausibly large orders (quantity 400-899) |
| A06 | injected_defect | 40 rows with a missing category |
| A07 | injected_defect | 30 rows with the wrong category, 20 with inconsistent case or whitespace |
| A08 | injected_anomaly | Customer C0777: about 150 small, 30%-discount orders of P020 in October |
| A09 | injected_anomaly | Apparel sales drop, 10 Feb to 9 Mar |
| A10 | injected_defect | 15 exact duplicate rows |
| A11 | ambiguous | 12 rows with negative quantity and revenue (returns, or errors) |
| L01 | legitimate_pattern | Black Friday week surge with deeper discounts |
| L02 | legitimate_pattern | Home & Garden spring seasonality |
| L03 | legitimate_pattern | Product P041 first sells on 1 Sep (launch) |

`kind` values:

- `injected_defect`: a data-quality error that should be flagged.
- `injected_anomaly`: an unusual pattern in the business data that should be flagged.
- `ambiguous`: has a defensible benign reading, so flagging it earns credit but missing it is reported separately from the other misses.
- `legitimate_pattern`: real business variation. Reporting it as a *problem* is a false positive; mentioning it as a trend is fine.

## Output contract

Each run produces `report.md`, `analysis.py`, `charts/` (at least two images) and `summary.json`. The `summary.json` structure is documented in `task/instructions.md` and enforced by `task/summary.schema.json`.

## How the evaluator matches findings

Deterministic rules only, no LLM judge. Each ground-truth entry has a list of alternative `match` rules. A finding matches an entry if it satisfies every condition of at least one rule:

- `segment`: the finding's `segment` has the same value for each listed key (case-insensitive).
- `date_overlap`: the finding's `segment.date_range` overlaps the given range.
- `transaction_ids_overlap_min`: the finding lists at least N of the entry's affected transaction IDs.
- `keywords_all` / `keywords_any`: terms found in the finding's description, evidence and uncertainty text. Every `keywords_all` term and at least one `keywords_any` term must appear.

An entry is *detected* if any finding matches it. A finding that matches no injected entry and no legitimate pattern counts as *unmatched*. A finding that matches only a legitimate pattern counts as a *legitimate pattern flagged*. Unmatched findings are reported for manual review rather than called false positives automatically, because the agent may have found a real trend the answer key doesn't list.

The matching is deliberately conservative and will miss unusual phrasings. Treat recall as a lower bound and read the per-finding match output when a number looks odd.
