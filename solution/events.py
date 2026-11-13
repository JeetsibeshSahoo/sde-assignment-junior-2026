"""
Event ingestion, ledger persistence, timestamp normalization, and orphan tracking.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from solution.leads import parse_phone
from solution.models import InteractionLedger, hash_phone

IST = timezone(timedelta(hours=5, minutes=30))


def parse_timestamp_ist(ts_str: str) -> datetime:
    """
    Parses timestamp into IST datetime:
    - UTC ('Z') converted to IST.
    - Offset ('+05:30') converted to IST.
    - Naive (e.g. '2026-10-03 10:01:00') treated as IST per spec.
    """
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        dt = datetime.fromisoformat(ts_str[:-1] + "+00:00")
        return dt.astimezone(IST)
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


class EventProcessor:
    def __init__(self, dispositions_csv_path: Path):
        self.dispositions: Dict[str, Set[str]] = {}
        with open(dispositions_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                wf = row["workflow_id"]
                code = row["code"]
                if wf not in self.dispositions:
                    self.dispositions[wf] = set()
                self.dispositions[wf].add(code)

    def process_jsonl_file(
        self,
        file_path: Path,
        session
    ) -> Dict[str, int]:
        """
        Reads one JSONL event file and persists valid, non-duplicate events
        to the append-only ledger.
        Returns metrics dict for this file.
        """
        stats = {
            "events_total": 0,
            "malformed_events": 0,
            "duplicate_events_dropped": 0,
            "unknown_disposition_events": 0,
        }

        # Cache existing event_ids in ledger for fast dedupe
        existing_event_ids: Set[str] = {
            e_id for (e_id,) in session.query(InteractionLedger.event_id).all()
        }

        # Cache known call_id -> phone_hash mappings
        call_to_phone: Dict[str, str] = {
            row.call_id: row.phone_hash
            for row in session.query(InteractionLedger.call_id, InteractionLedger.phone_hash).filter(
                InteractionLedger.phone_hash.isnot(None)
            ).all()
        }

        new_events: List[InteractionLedger] = []

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue

                stats["events_total"] += 1

                # 1. Check malformed JSON
                try:
                    data = json.loads(line)
                except Exception:
                    stats["malformed_events"] += 1
                    continue

                event_id = data.get("event_id")
                if not event_id:
                    stats["malformed_events"] += 1
                    continue

                # 2. Check duplicate event_id
                if event_id in existing_event_ids:
                    stats["duplicate_events_dropped"] += 1
                    continue

                workflow_id = data.get("workflow_id", "")
                event_type = data.get("event_type", "")
                call_id = data.get("call_id", "")
                occurred_at_str = data.get("occurred_at", "")
                payload = data.get("payload", {})
                raw_phone = data.get("phone")

                # 3. Check unknown disposition
                if event_type in ("disposition.set", "disposition.override"):
                    disp_code = payload.get("disposition_code")
                    valid_codes = self.dispositions.get(workflow_id, set())
                    if disp_code not in valid_codes:
                        stats["unknown_disposition_events"] += 1
                        continue

                # Parse timestamp
                try:
                    occurred_at = parse_timestamp_ist(occurred_at_str)
                except Exception:
                    stats["malformed_events"] += 1
                    continue

                # Determine phone_hash
                phone_hash = None
                if raw_phone:
                    canonical = parse_phone(str(raw_phone))
                    if canonical:
                        phone_hash = hash_phone(canonical)
                        call_to_phone[call_id] = phone_hash
                else:
                    # e.g., disposition.override links through call_id
                    if call_id in call_to_phone:
                        phone_hash = call_to_phone[call_id]

                event_obj = InteractionLedger(
                    event_id=event_id,
                    event_type=event_type,
                    workflow_id=workflow_id,
                    call_id=call_id,
                    phone_hash=phone_hash,
                    occurred_at=occurred_at,
                    payload=payload
                )

                new_events.append(event_obj)
                existing_event_ids.add(event_id)

        # Backfill any overrides that had no phone_hash if call_to_phone now has it
        for ev in new_events:
            if ev.phone_hash is None and ev.call_id in call_to_phone:
                ev.phone_hash = call_to_phone[ev.call_id]

        session.add_all(new_events)
        session.commit()

        # Also update any previously ingested ledger records for this call_id
        for call_id, p_hash in call_to_phone.items():
            session.query(InteractionLedger).filter(
                InteractionLedger.call_id == call_id,
                InteractionLedger.phone_hash.is_(None)
            ).update({"phone_hash": p_hash}, synchronize_session=False)
        session.commit()

        return stats