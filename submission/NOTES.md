# Submission Notes

## What this solution does

This submission uses a deterministic, standard-library pipeline. It ingests every website snapshot, creates heading-aware parent/child chunks, builds a separate BM25 index for each snapshot, and routes each question to one eligible snapshot before retrieving evidence.

The current answer layer supports the rate schemas that were verified during development. It returns only the requested metric and the identifiers needed to show which row was selected. If a safe exact projection is unavailable, it returns a short non-confident refusal instead of raw page text.

## Outdated auto-calculator rate handling

The auto-loan decision is data-driven:

1. Route an undated question to the latest snapshot. Explicit dates route to the newest snapshot on or before that date; `previous rate` routes to the preceding eligible snapshot.
2. Extract the requested term and model year.
3. Retrieve matching rows and validate the term and model-year range.
4. Classify each source from its table schema and page context. Official auto-product/rate pages are eligible answer sources; calculator pages are not.
5. Prefer the strongest matching official source and return its APR with its real URL, snapshot ID, and supporting row.
6. Declare a calculator conflict only when calculator and official rows have the same term, overlapping model years, different APRs, and the official product page has a newer effective date.

This means 5.39% is selected for the current 60-month 2025-model-year question because the official auto-product table is effective 2026-08-31, while the calculator table that shows 5.34% is effective 2026-01-01. The percentages themselves are read from the snapshots; they are not hardcoded in the solution.

## Other verified behavior

Certificate rows use semantic header aliases, audience checks, term/tier validation, and source-role ranking. The implementation recognizes changed values, renamed supported columns, and moved official URLs in the included future-data probes.

Member-facing answers do not include unrequested sibling rates, minimum balances, raw Markdown, retrieval scores, or stale alternatives. Full evidence remains in `Answer.sources`. Known calculator/product disagreements are available through `conflicts()` with both values, both sources, the selected resolution, and the reason.

## Runtime and cost

- Python standard library only
- No API keys
- No runtime network calls
- No paid services
- No local LLM or model download
- Expected API cost: $0

## Validation

Run the public harness from the repository root:

```powershell
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl --out results/final
```

Final measured public result:

- QA/final score: 0.1938
- Questions: 40
- Stale trap hits: 0
- Errors: 0
- Refusal cases passed: 3 of 3
- Consistency: 1.0
- p95 answer latency: 0.14 seconds
- Ingestion: 4.3 seconds

Additional local probes cover schema aliases, source roles, moved URLs, changed future values, and calculator conflict extraction.

## Current limitation

The safe-answer policy is intentionally conservative. Questions outside a verified structured handler are refused. This avoids unsupported numbers and extra content, but it leaves broader product, branch, routing, mortgage, and list-style coverage for future work.
