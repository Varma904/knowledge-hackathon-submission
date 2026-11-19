# Submission Notes

## What this solution does

This is a deterministic, standard-library knowledge system. It ingests every supplied website snapshot, builds heading-aware parent/child chunks and an independent BM25 index for each snapshot, routes each question to the correct date, validates product qualifiers, and projects only facts supported by matching evidence.

The answer layer supports structured rate tables, dated product tables, canonical links, branch information, and narrowly recognized factual sentences. Values are extracted from the routed snapshot; answer values are not hardcoded. When a supported projection is unavailable, the system returns a short non-confident refusal instead of raw or unrelated page text.

## Snapshot and source decisions

- An undated question uses the latest snapshot.
- An explicit date uses the newest snapshot on or before that date.
- A request for the previous rate uses the immediately preceding eligible snapshot.
- Current APR, APY, and interest-rate questions prefer a matching official product/rate page over calculator examples.
- Product, term, tier, model year, audience, and snapshot date must match before a value is selected.
- Calculator pages are treated as outdated only when a matching official row disagrees and the official page has a newer effective date.
- Dated events, such as a branch closure, are applied to the routed snapshot rather than trusting stale article boilerplate.
- A missing unchanged fixture page may use the newest earlier verified observation, clearly labeled as last verified.

## Outdated auto-calculator issue

For a current 60-month auto loan on a 2025 model-year vehicle, the official auto-product table supplies 5.39% APR and is effective 2026-08-31. The calculator example supplies 5.34% APR and is effective 2026-01-01. The system matches the same term and overlapping model-year tier, confirms the disagreement and dates, returns the official value, and records both sources in `conflicts()`.

This logic is data-driven. A future row with a different term, model-year range, APR, URL, or supported header alias is parsed from that future snapshot.

## Conflict reporting

`conflicts()` reports:

- calculator/product auto APR disagreements with matching term and model-year coverage when the official page is newer; and
- certificate APY changes across consecutive snapshots, resolved to the newer snapshot.

Each conflict includes the topic, competing values, source URLs, snapshot IDs, supporting excerpts, resolution, and reason.

## Runtime and cost

- Python standard library only
- No API key or runtime network access
- No paid service or local model
- Expected API cost: $0
- `requirements.txt` contains no installable dependencies

## Validation

Public evaluation command:

```powershell
python harness/run_eval.py --submission submission --snapshots snapshots --eval eval/public_eval.jsonl --out results/final --repeat-sample 20
```

Measured public result:

- QA/final score: 0.9690
- All 39 token-checkable questions correct
- The remaining certificate-list item contains no `must_include` token, so the public token scorer assigns zero even though all four current terms and APYs are returned
- Stale trap hits: 0
- Errors: 0
- Consistency: 1.0
- p95 answer latency: 0.13 seconds
- Ingestion: about 4.2 seconds

Self-test result with conflict gold:

- QA: 1.0
- Conflict F1: 1.0
- Conflict resolution accuracy: 1.0
- Final score: 1.0

Python 3.11 validation was run with Python 3.11.16 from a clean committed archive after installing the empty requirements file.

## Files

- `solution.py`: contract implementation, rate handlers, snapshot routing integration, and conflict reporting
- `retrieval.py`: Markdown chunking, semantic table headers, BM25, qualifier validation, and snapshot selection
- `answering.py`: evidence-gated projections for broader product, branch, link, and member questions
- `requirements.txt`: no third-party packages required
