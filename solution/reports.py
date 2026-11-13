"""
Report generator producing the 5 contract output files:
- batch_summary.csv
- daily_overview.csv
- lead_report.csv
- rejections.csv
- data_quality.json
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from solution.models import Lead, InteractionLedger, Rejection, IST


class ReportGenerator:
    def __init__(self, dispositions_csv_path: Path):
        self.disp_info: Dict[Tuple[str, str], Dict[str, bool]] = {}
        with open(dispositions_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                wf = row["workflow_id"]
                code = row["code"]
                is_term = row["is_terminal"].strip().lower() == "true"
                is_succ = row["is_success"].strip().lower() == "true"
                self.disp_info[(wf, code)] = {
                    "is_terminal": is_term,
                    "is_success": is_succ,
                }

    def generate_all(self, session, out_dir: Path, data_quality_stats: Dict[str, Any]):
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Fetch all leads
        leads: List[Lead] = session.query(Lead).all()
        leads_by_identity: Dict[Tuple[str, str], Lead] = {
            (l.workflow_id, l.phone_hash): l for l in leads
        }

        # 2. Fetch all ledger events
        events: List[InteractionLedger] = (
            session.query(InteractionLedger)
            .order_by(InteractionLedger.occurred_at.asc())
            .all()
        )

        # Group events by call_id
        calls: Dict[str, Dict[str, Any]] = {}
        for ev in events:
            c_id = ev.call_id
            if c_id not in calls:
                calls[c_id] = {
                    "call_id": c_id,
                    "workflow_id": ev.workflow_id,
                    "phone_hash": ev.phone_hash,
                    "initiated_at": None,
                    "connected": False,
                    "telephony_failed": False,
                    "dispositions": [],  # (occurred_at, code, is_override)
                }

            c = calls[c_id]
            if ev.phone_hash and not c["phone_hash"]:
                c["phone_hash"] = ev.phone_hash

            if ev.event_type == "call.initiated":
                c["initiated_at"] = ev.occurred_at
            elif ev.event_type == "call.connected":
                c["connected"] = True
            elif ev.event_type == "call.telephony_failed":
                c["telephony_failed"] = True
            elif ev.event_type == "disposition.set":
                disp_code = ev.payload.get("disposition_code")
                if disp_code:
                    c["dispositions"].append((ev.occurred_at, disp_code, False))
            elif ev.event_type == "disposition.override":
                disp_code = ev.payload.get("disposition_code")
                if disp_code:
                    c["dispositions"].append((ev.occurred_at, disp_code, True))

        # Filter valid calls: must have call.initiated and linked to a known lead
        valid_calls: List[Dict[str, Any]] = []
        lead_calls_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
            k: [] for k in leads_by_identity
        }

        for c_id, c in calls.items():
            if c["initiated_at"] is None:
                continue
            wf = c["workflow_id"]
            ph = c["phone_hash"]
            lead_key = (wf, ph)
            if lead_key in leads_by_identity:
                # Compute effective disposition
                # Overrides take precedence; latest by occurred_at
                overrides = [d for d in c["dispositions"] if d[2]]
                regulars = [d for d in c["dispositions"] if not d[2]]

                eff_disp = None
                if overrides:
                    overrides.sort(key=lambda x: x[0])
                    eff_disp = overrides[-1][1]
                elif regulars:
                    regulars.sort(key=lambda x: x[0])
                    eff_disp = regulars[-1][1]

                c["effective_disposition"] = eff_disp
                valid_calls.append(c)
                lead_calls_map[lead_key].append(c)

        # -------------------------------------------------------------
        # Report 1: batch_summary.csv
        # -------------------------------------------------------------
        # Calculate buckets for each lead
        lead_buckets: Dict[Tuple[str, str], str] = {}
        for lead_key, lead in leads_by_identity.items():
            l_calls = lead_calls_map[lead_key]
            wf = lead.workflow_id
            if len(l_calls) == 0:
                lead_buckets[lead_key] = "yet_to_reach_out"
            elif any(
                c["effective_disposition"]
                and self.disp_info.get((wf, c["effective_disposition"]), {}).get("is_terminal")
                for c in l_calls
            ):
                lead_buckets[lead_key] = "completed"
            elif all(c["telephony_failed"] for c in l_calls):
                lead_buckets[lead_key] = "in_progress_telephony_failed"
            else:
                lead_buckets[lead_key] = "in_progress_dialed"

        # Aggregate by (workflow_id, batch_id)
        batch_stats: Dict[Tuple[str, str], Dict[str, int]] = {}
        for (wf, ph), lead in leads_by_identity.items():
            b_key = (wf, lead.batch_id)
            if b_key not in batch_stats:
                batch_stats[b_key] = {
                    "total_leads": 0,
                    "yet_to_reach_out": 0,
                    "in_progress_telephony_failed": 0,
                    "in_progress_dialed": 0,
                    "completed": 0,
                }
            batch_stats[b_key]["total_leads"] += 1
            bucket = lead_buckets[(wf, ph)]
            batch_stats[b_key][bucket] += 1

        batch_rows = []
        for (wf, batch_id), stats in batch_stats.items():
            batch_rows.append({
                "workflow_id": wf,
                "batch_id": batch_id,
                "total_leads": stats["total_leads"],
                "yet_to_reach_out": stats["yet_to_reach_out"],
                "in_progress_telephony_failed": stats["in_progress_telephony_failed"],
                "in_progress_dialed": stats["in_progress_dialed"],
                "completed": stats["completed"],
            })
        batch_rows.sort(key=lambda x: (x["workflow_id"], x["batch_id"]))

        with open(out_dir / "batch_summary.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "workflow_id", "batch_id", "total_leads",
                    "yet_to_reach_out", "in_progress_telephony_failed",
                    "in_progress_dialed", "completed"
                ]
            )
            writer.writeheader()
            writer.writerows(batch_rows)

        # -------------------------------------------------------------
        # Report 2: daily_overview.csv
        # -------------------------------------------------------------
        daily_stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for c in valid_calls:
            wf = c["workflow_id"]
            init_dt = c["initiated_at"].astimezone(IST)
            date_str = init_dt.strftime("%Y-%m-%d")
            d_key = (wf, date_str)

            if d_key not in daily_stats:
                daily_stats[d_key] = {
                    "total_calls": 0,
                    "connected_calls": 0,
                    "success_leads": set(),
                }

            daily_stats[d_key]["total_calls"] += 1
            if c["connected"]:
                daily_stats[d_key]["connected_calls"] += 1

            eff = c["effective_disposition"]
            if eff and self.disp_info.get((wf, eff), {}).get("is_success"):
                daily_stats[d_key]["success_leads"].add(c["phone_hash"])

        daily_rows = []
        for (wf, dt_str), ddata in daily_stats.items():
            tot = ddata["total_calls"]
            conn = ddata["connected_calls"]
            rate_str = f"{(conn / tot):.4f}" if tot > 0 else "0.0000"
            daily_rows.append({
                "workflow_id": wf,
                "date": dt_str,
                "total_calls": tot,
                "connected_calls": conn,
                "connectivity_rate": rate_str,
                "success_leads": len(ddata["success_leads"]),
            })
        daily_rows.sort(key=lambda x: (x["workflow_id"], x["date"]))

        with open(out_dir / "daily_overview.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "workflow_id", "date", "total_calls", "connected_calls",
                    "connectivity_rate", "success_leads"
                ]
            )
            writer.writeheader()
            writer.writerows(daily_rows)

        # -------------------------------------------------------------
        # Report 3: lead_report.csv
        # -------------------------------------------------------------
        lead_report_rows = []
        for (wf, ph), lead in leads_by_identity.items():
            l_calls = lead_calls_map[(wf, ph)]
            l_calls.sort(key=lambda x: x["initiated_at"])

            total_calls = len(l_calls)
            connected_calls = sum(1 for c in l_calls if c["connected"])
            last_call_at = (
                l_calls[-1]["initiated_at"].astimezone(IST).isoformat()
                if total_calls > 0 else ""
            )

            # current_disposition: effective disposition of most recent call that has one
            calls_with_disp = [c for c in l_calls if c["effective_disposition"]]
            current_disp = calls_with_disp[-1]["effective_disposition"] if calls_with_disp else ""

            # ever_success
            ever_success = any(
                c["effective_disposition"]
                and self.disp_info.get((wf, c["effective_disposition"]), {}).get("is_success")
                for c in l_calls
            )

            lead_report_rows.append({
                "workflow_id": wf,
                "external_ref": lead.external_ref,
                "batch_id": lead.batch_id,
                "phone_masked": lead.phone_masked,
                "bucket": lead_buckets[(wf, ph)],
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "last_call_at": last_call_at,
                "current_disposition": current_disp,
                "ever_success": "true" if ever_success else "false",
            })
        lead_report_rows.sort(key=lambda x: (x["workflow_id"], x["external_ref"]))

        with open(out_dir / "lead_report.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "workflow_id", "external_ref", "batch_id", "phone_masked",
                    "bucket", "total_calls", "connected_calls", "last_call_at",
                    "current_disposition", "ever_success"
                ]
            )
            writer.writeheader()
            writer.writerows(lead_report_rows)

        # -------------------------------------------------------------
        # Report 4: rejections.csv
        # -------------------------------------------------------------
        rejections: List[Rejection] = (
            session.query(Rejection)
            .order_by(Rejection.workflow_id, Rejection.batch_id, Rejection.row_number)
            .all()
        )
        with open(out_dir / "rejections.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["workflow_id", "batch_id", "row_number", "reason"]
            )
            writer.writeheader()
            for r in rejections:
                writer.writerow({
                    "workflow_id": r.workflow_id,
                    "batch_id": r.batch_id,
                    "row_number": r.row_number,
                    "reason": r.reason
                })

        # -------------------------------------------------------------
        # Report 5: data_quality.json
        # -------------------------------------------------------------
        # Orphan events: non-duplicate ledger events that cannot be linked to any valid lead
        orphan_count = 0
        for ev in events:
            if not ev.phone_hash or (ev.workflow_id, ev.phone_hash) not in leads_by_identity:
                orphan_count += 1

        final_dq = {
            "lead_rows_total": data_quality_stats.get("lead_rows_total", 0),
            "lead_rows_rejected": data_quality_stats.get("lead_rows_rejected", 0),
            "rejections_by_reason": data_quality_stats.get("rejections_by_reason", {}),
            "duplicate_lead_rows_merged": data_quality_stats.get("duplicate_lead_rows_merged", 0),
            "field_warnings": data_quality_stats.get("field_warnings", 0),
            "events_total": data_quality_stats.get("events_total", 0),
            "malformed_events": data_quality_stats.get("malformed_events", 0),
            "duplicate_events_dropped": data_quality_stats.get("duplicate_events_dropped", 0),
            "orphan_events": orphan_count,
            "unknown_disposition_events": data_quality_stats.get("unknown_disposition_events", 0),
        }

        with open(out_dir / "data_quality.json", "w", encoding="utf-8") as f:
            json.dump(final_dq, f, indent=2)