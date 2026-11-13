# AI Usage Disclosure

## Tools used
Antigravity (Gemini 3.8 Flash)

## What I used them for
- Discussing problem choice and comparing Problem A vs Problem B under the time constraints.
- Scaffolding the initial SQLite/PostgreSQL schema and phone normalization regular expressions.
- Drafting the unit tests in `tests/test_pipeline.py`.
- Formulating the documentation in `SUBMISSION.md`.

## What I wrote or decided myself
- Selected Problem A based on determinism, reliability, and interview defensibility.
- Architecture decisions: using SQLAlchemy for dual PostgreSQL/SQLite support so the project runs cleanly without external dependencies.
- PII strategy: using salted SHA-256 HMAC for exact relational matching without plaintext phone storage, coupled with `+91******XXXX` masking for reports.
- Idempotency design: using an `ingested_files` table to guarantee that re-running drops produces identical output and does not double-count metrics.
- Validated all 5 generated report files against the contract specifications.

## Where the AI was wrong or unhelpful
- Initial test execution failed with `ModuleNotFoundError` because `pytest` did not have the root folder in `sys.path`. Resolved by creating a standard `pytest.ini` with `pythonpath = .`.

## How I verified AI-generated code
- Tested each module individually: `leads.py`, `events.py`, and `reports.py`.
- Verified that re-running `ingest data/drop_1` skipped already ingested files.
- Verified that all report metrics and numbers matched the mathematical sums (e.g. `yet_to_reach_out + completed + in_progress_telephony_failed + in_progress_dialed == total_leads`).
- Ran the full `pytest` suite and confirmed 100% pass rate.