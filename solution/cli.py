"""
CLI entry point for:
  python -m solution ingest <drop_path>
  python -m solution report --out <output_dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Any

from sqlalchemy import func

from solution.events import EventProcessor
from solution.leads import LeadProcessor
from solution.models import (
    DataQualityCounter,
    IngestedFile,
    Rejection,
    get_engine,
    init_db,
)
from solution.reports import ReportGenerator


def get_file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def update_counter(session, name: str, delta: int):
    c = session.query(DataQualityCounter).filter_by(metric_name=name).first()
    if not c:
        c = DataQualityCounter(metric_name=name, metric_value=delta)
        session.add(c)
    else:
        c.metric_value += delta
    session.commit()


def get_all_counters(session) -> Dict[str, Any]:
    counters = {}
    for row in session.query(DataQualityCounter).all():
        counters[row.metric_name] = row.metric_value
    return counters


def handle_ingest(drop_path_str: str):
    drop_path = Path(drop_path_str)
    if not drop_path.exists():
        print(f"Error: Drop path '{drop_path}' does not exist.")
        return

    # Locate schema files
    root = Path(__file__).resolve().parent.parent
    schemas_dir = root / "assignment-a-data" / "schemas"
    dispositions_path = schemas_dir / "dispositions.csv"

    engine = get_engine()
    session = init_db(engine)

    lead_proc = LeadProcessor(schemas_dir)
    event_proc = EventProcessor(dispositions_path)

    # 1. Process Leads
    # Rules: "process drops in the order ingested, files in filename order and rows in file order"
    leads_dir = drop_path / "leads"
    if leads_dir.exists():
        wf_dirs = sorted([d for d in leads_dir.iterdir() if d.is_dir()])
        for wf_dir in wf_dirs:
            wf_id = wf_dir.name
            csv_files = sorted(list(wf_dir.glob("*.csv")))
            for csv_file in csv_files:
                f_key = f"{drop_path.name}/leads/{wf_id}/{csv_file.name}"
                f_hash = get_file_hash(csv_file)

                # Idempotency check
                already_done = session.query(IngestedFile).filter_by(file_path=f_key).first()
                if already_done:
                    print(f"Skipping already ingested file: {f_key}")
                    continue

                total, rejected, rej_reasons, merged, warnings = lead_proc.process_csv_file(
                    csv_file, wf_id, session
                )

                update_counter(session, "lead_rows_total", total)
                update_counter(session, "lead_rows_rejected", rejected)
                update_counter(session, "duplicate_lead_rows_merged", merged)
                update_counter(session, "field_warnings", warnings)

                session.add(IngestedFile(file_path=f_key, file_hash=f_hash))
                session.commit()
                print(f"Ingested leads from {csv_file.name} ({wf_id}): {total} rows ({rejected} rejected, {merged} merged)")

    # 2. Process Events
    events_dir = drop_path / "events"
    if events_dir.exists():
        jsonl_files = sorted(list(events_dir.glob("*.jsonl")))
        for j_file in jsonl_files:
            f_key = f"{drop_path.name}/events/{j_file.name}"
            f_hash = get_file_hash(j_file)

            already_done = session.query(IngestedFile).filter_by(file_path=f_key).first()
            if already_done:
                print(f"Skipping already ingested file: {f_key}")
                continue

            stats = event_proc.process_jsonl_file(j_file, session)

            update_counter(session, "events_total", stats["events_total"])
            update_counter(session, "malformed_events", stats["malformed_events"])
            update_counter(session, "duplicate_events_dropped", stats["duplicate_events_dropped"])
            update_counter(session, "unknown_disposition_events", stats["unknown_disposition_events"])

            session.add(IngestedFile(file_path=f_key, file_hash=f_hash))
            session.commit()
            print(f"Ingested events from {j_file.name}: {stats['events_total']} lines")

    session.close()
    print(f"Successfully finished ingest for: {drop_path}")


def handle_report(out_dir_str: str):
    out_dir = Path(out_dir_str)
    root = Path(__file__).resolve().parent.parent
    dispositions_path = root / "assignment-a-data" / "schemas" / "dispositions.csv"

    engine = get_engine()
    session = init_db(engine)

    counters = get_all_counters(session)

    # Rejections by reason from database
    rej_counts = {}
    for r_reason, count in session.query(Rejection.reason, func.count(Rejection.id)).group_by(Rejection.reason).all():
        rej_counts[r_reason] = count
    counters["rejections_by_reason"] = rej_counts

    generator = ReportGenerator(dispositions_path)
    generator.generate_all(session, out_dir, counters)
    session.close()

    print(f"Reports successfully generated in '{out_dir}/':")
    for f in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        p = out_dir / f
        if p.exists():
            print(f"  - {f} ({p.stat().st_size} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Agentic Universe Take-Home Pipeline (Problem A)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest subcommand
    ingest_p = subparsers.add_parser("ingest", help="Ingest a data drop folder")
    ingest_p.add_argument("drop_path", help="Path to drop folder (e.g. data/drop_1)")

    # Report subcommand
    report_p = subparsers.add_parser("report", help="Generate all reports")
    report_p.add_argument("--out", required=True, help="Directory to save report files")

    args = parser.parse_args()
    if args.command == "ingest":
        handle_ingest(args.drop_path)
    elif args.command == "report":
        handle_report(args.out)


if __name__ == "__main__":
    main()