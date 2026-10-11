# Task: Sales Data Analyst

Analyze the supplied sales transactions. Explore the dataset, identify meaningful trends and anomalies, investigate anything suspicious, generate useful visualizations, and produce a concise report explaining your findings.

Write and execute Python code as needed. Inspect the results of your analysis and revise your approach when necessary.

You decide how to approach the investigation and when the analysis is sufficiently complete.

## Data

One CSV file of sales transactions covering a single year. Columns:

| Column | Meaning |
|---|---|
| transaction_id | Transaction identifier |
| date | Transaction date (YYYY-MM-DD) |
| customer_id | Customer identifier |
| product_id | Product identifier |
| category | Product category |
| region | Sales region |
| quantity | Units sold |
| unit_price | Price per unit |
| discount_pct | Discount percentage |
| revenue | Recorded transaction revenue |

The data may contain quality problems. Some unusual patterns are genuine business variation, not errors. Use judgement and say how sure you are.

## Deliverables

Write all of these to the output directory named in the Environment section.

1. `report.md`: a concise report with an executive summary, important sales trends, identified anomalies with supporting numbers, references to your charts, uncertainty and limitations, and recommended follow-up investigations.
2. `analysis.py`: a script containing your analysis code. It must run on the original CSV on its own, without this session.
3. `charts/`: at least two useful charts as PNG files.
4. `summary.json`: your findings in the structure below.

### summary.json

```json
{
  "findings": [
    {
      "finding_id": "F1",
      "description": "What you found, in one or two sentences.",
      "segment": {
        "region": "East",
        "category": "Furniture",
        "product_id": "P001",
        "customer_id": "C0002",
        "date_range": ["2025-05-01", "2025-05-31"],
        "transaction_ids": ["T000123", "T000456"]
      },
      "evidence": [
        {"metric": "total_revenue", "value": 123456.78, "note": "optional context"}
      ],
      "confidence": "high",
      "uncertainty": "Optional note on what could make this wrong.",
      "further_investigation_recommended": true
    }
  ],
  "limitations": ["Optional list of caveats about the analysis as a whole."]
}
```

- `finding_id`, `description`, `segment`, `evidence`, `confidence` and `further_investigation_recommended` are required on every finding.
- `segment` identifies the part of the data the finding is about. Include only the keys that apply; use `{}` for dataset-wide findings. `transaction_ids` may list a representative sample.
- `confidence` is one of `"high"`, `"medium"`, `"low"`.
- `evidence` is a list of numbers you computed. Use a plain `metric` name and a numeric or string `value`.
- Report anomalies and data defects, and also notable trends. Do not pad the list; do not omit something you believe is real.
