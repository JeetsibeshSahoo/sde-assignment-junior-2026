# Submission

## Candidate
- **Name:** [Your Name]
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~4h

## How to run
```bash
# 1. Environment setup
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 2. Ingest drops
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2

# 3. Generate reports
python -m solution report --out reports

# 4. Run automated test suite
pytest
```

## Design
The pipeline is designed as an idempotent, append-only data processing and reporting engine built on SQLAlchemy (supporting both local SQLite for zero-setup execution and PostgreSQL for production scale).

### Database Schema
1. **`leads` Table:**
   - Identity: `(workflow_id, phone_hash)`.
   - PII Protection: Plaintext phone numbers and customer names are not stored in plaintext at rest. The phone is stored as a deterministic SHA-256 HMAC hash (`phone_hash`) for fast $O(1)$ relational joins, and masked as `+91******XXXX` (`phone_masked`) for reporting.
   - Merging: The first valid record establishes the permanent `batch_id`. Subsequent records update non-null fields (last-write-wins). Schema defaults are applied post-merge.
2. **`interaction_ledger` Table (Append-Only Event Store):**
   - Stores all telephony and disposition events (`call.initiated`, `call.connected`, `disposition.set`, `disposition.override`, etc.).
   - Overrides never mutate or delete earlier records; they are inserted as new events with the latest `occurred_at`.
   - Bidirectional resolution: `disposition.override` records (which lack phone numbers) link to leads via `call_id` to matching call events.
3. **`rejections` Table:**
   - Captures invalid rows with exact `row_number` and `reason` (e.g. `invalid_phone`, `missing_required:customer_name`) without leaking PII.
4. **`ingested_files` Table:**
   - Tracks processed file paths and SHA-256 checksums to guarantee idempotency across repeated ingest runs.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Deterministic HMAC-SHA256 for phone lookup | Allows fast relational joins and deduplication without storing plaintext PII | Reversible symmetric encryption (AES) - introduces unnecessary key management overhead for an offline pipeline |
| Dual-Engine SQLAlchemy (PostgreSQL + SQLite) | Reviewers can run tests immediately without starting Docker, while production PostgreSQL is supported via `DATABASE_URL` | SQLite only or PostgreSQL only |
| Naive timestamps interpreted as IST | Spec explicitly states naive timestamps represent IST | Discarding naive timestamps or assuming UTC |
| Bidirectional override linking | Events can arrive in arbitrary order (override before call.initiated or vice-versa) | Requiring strictly ordered event arrival |
| File-level idempotency tracking in DB | Ensures re-running `ingest data/drop_1` changes nothing and does not double-count metrics | In-memory tracking (resets on process restart) |

## Failure modes considered
- **Dirty & Malformed Data:** Handled gracefully. Unparseable JSON and missing event IDs increment `malformed_events`. Invalid phone numbers and missing required fields are routed to `rejections.csv`.
- **Re-runs / Idempotency:** The `ingested_files` table prevents duplicate processing of already ingested CSV and JSONL files.
- **Orphan Events:** Events arriving before leads are safely stored in the ledger; when the corresponding lead arrives in a later drop, the relational link resolves automatically.
- **Out of Order Events:** Timestamps are normalized to timezone-aware IST datetimes and sorted at query time.

## Testing
- Automated test suite (`pytest`) covering:
  - Phone normalisation (valid prefixes 6-9, 10/11/12-digit rules, non-digit stripping).
  - Day-first date parsing (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-Mon-YYYY`) and rejection of impossible dates (`31/02`).
  - Currency and decimal parsing (cleaning currency symbols `₹`, `Rs`, `INR`, thousands commas).
  - Header normalisation and schema alias resolution.
  - PII masking and deterministic hashing.
  - IST timestamp normalisation (UTC `Z`, offset, and naive IST).
  - Bucket logic priority order.

## Results
- Ingesting `drop_1` + `drop_2` completed in **~1.5 seconds**.
- Data quality verification on Seed 42:
  - Total lead rows: 2,057 (108 rejected, 57 duplicate rows merged, 122 field warnings).
  - Total events: 8,745 (8 malformed, 235 duplicates dropped, 21 unknown dispositions, 461 orphans).
  - All mathematical metrics (`batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`) verified against contract specifications.

## If I had another day
1. Implement DuckDB or pure SQL aggregations for report generation on large-scale datasets (10M+ rows).
2. Add Apache Arrow / Polars streaming reader for high-throughput batch ingestion.
3. Build a FastAPI admin dashboard endpoint to view data quality metrics and pipeline runs.