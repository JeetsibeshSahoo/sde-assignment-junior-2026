"""
Lead ingestion, schema validation, normalization, and duplicate merging.
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from solution.models import Lead, Rejection, hash_phone, mask_phone


def normalize_header(header: str) -> str:
    """
    Lower-case, trim, and replace every run of '_' or whitespace with a single space.
    """
    if not header:
        return ""
    cleaned = re.sub(r"[_\s]+", " ", header.strip().lower())
    return cleaned


def parse_phone(val: str) -> Optional[str]:
    """
    Normalizes Indian mobile number to canonical '+91XXXXXXXXXX'.
    Rules:
    - Strip all non-digits.
    - If 12 digits starting with '91', drop '91'.
    - If 11 digits starting with '0', drop '0'.
    - Must be exactly 10 digits starting with 6, 7, 8, or 9.
    """
    digits = re.sub(r"\D", "", val)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(val: str) -> Optional[str]:
    """
    Accepts day-first dates: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    Returns canonical YYYY-MM-DD or None if invalid.
    """
    val = val.strip()
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(val, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_decimal(val: str) -> Optional[float]:
    """
    Parses currency/decimal value, optionally prefixed by ₹, Rs, Rs., or INR.
    Commas are thousands separators. Must be >= 0. Rounded to 2 decimals.
    """
    s = val.strip()
    # Remove prefix (case-insensitive)
    s = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE)
    # Remove commas
    s = s.replace(",", "").strip()
    try:
        f = float(s)
        if f >= 0:
            return round(f, 2)
    except ValueError:
        pass
    return None


def parse_enum(val: str, allowed_values: List[str]) -> Optional[str]:
    """
    Case-insensitive match against allowed values. Returns lowercase string or None.
    """
    s = val.strip().lower()
    if s in [v.lower() for v in allowed_values]:
        return s
    return None


class LeadProcessor:
    def __init__(self, schemas_dir: Path):
        self.schemas: Dict[str, Dict[str, Any]] = {}
        for schema_file in schemas_dir.glob("*.json"):
            with open(schema_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.schemas[data["workflow_id"]] = data

    def process_csv_file(
        self,
        file_path: Path,
        workflow_id: str,
        session
    ) -> Tuple[int, int, Dict[str, int], int, int]:
        """
        Processes a single lead CSV file:
        Returns:
            (rows_total, rows_rejected, rejections_by_reason, merged_duplicates, field_warnings)
        """
        schema = self.schemas.get(workflow_id)
        if not schema:
            raise ValueError(f"Unknown workflow: {workflow_id}")

        batch_id = file_path.stem  # e.g., 'batch_001'

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header_row = next(reader, None)
            if header_row is None:
                return 0, 0, {}, 0, 0

            # Map normalized headers to column index
            header_map: Dict[str, int] = {}
            for idx, raw_h in enumerate(header_row):
                norm_h = normalize_header(raw_h)
                for field in schema["fields"]:
                    candidates = [normalize_header(field["name"])] + [
                        normalize_header(a) for a in field.get("aliases", [])
                    ]
                    if norm_h in candidates and field["name"] not in header_map:
                        header_map[field["name"]] = idx
                        break

            rows_total = 0
            rows_rejected = 0
            rejections_by_reason: Dict[str, int] = {}
            merged_duplicates = 0
            field_warnings = 0

            for raw_row in reader:
                # Check for blank rows (all empty/whitespace)
                if not raw_row or all(not cell.strip() for cell in raw_row):
                    continue

                rows_total += 1
                row_number = rows_total

                # Extract and validate fields in schema order
                rejection_reason = None
                parsed_values: Dict[str, Any] = {}
                row_warnings = 0

                for field in schema["fields"]:
                    fname = field["name"]
                    ftype = field["type"]
                    is_req = field.get("required", False)

                    col_idx = header_map.get(fname)
                    raw_val = raw_row[col_idx].strip() if (col_idx is not None and col_idx < len(raw_row)) else ""

                    if not raw_val:
                        if is_req:
                            rejection_reason = f"missing_required:{fname}"
                            break
                        else:
                            parsed_values[fname] = None
                            continue

                    # Validate present value
                    if ftype == "phone":
                        canonical = parse_phone(raw_val)
                        if not canonical:
                            rejection_reason = "invalid_phone"
                            break
                        parsed_values[fname] = canonical

                    elif ftype == "string":
                        parsed_values[fname] = raw_val

                    elif ftype == "date":
                        dt = parse_date(raw_val)
                        if not dt:
                            if is_req:
                                rejection_reason = f"invalid_date:{fname}"
                                break
                            else:
                                row_warnings += 1
                                parsed_values[fname] = None
                        else:
                            parsed_values[fname] = dt

                    elif ftype == "decimal":
                        dec = parse_decimal(raw_val)
                        if dec is None:
                            if is_req:
                                rejection_reason = f"invalid_decimal:{fname}"
                                break
                            else:
                                row_warnings += 1
                                parsed_values[fname] = None
                        else:
                            parsed_values[fname] = dec

                    elif ftype == "enum":
                        en = parse_enum(raw_val, field.get("values", []))
                        if not en:
                            if is_req:
                                rejection_reason = f"invalid_enum:{fname}"
                                break
                            else:
                                row_warnings += 1
                                parsed_values[fname] = None
                        else:
                            parsed_values[fname] = en

                # If rejected, record rejection and continue
                if rejection_reason:
                    rows_rejected += 1
                    rejections_by_reason[rejection_reason] = rejections_by_reason.get(rejection_reason, 0) + 1
                    session.add(Rejection(
                        workflow_id=workflow_id,
                        batch_id=batch_id,
                        row_number=row_number,
                        reason=rejection_reason
                    ))
                    continue

                # Not rejected: count field warnings
                field_warnings += row_warnings

                # Determine identity: (workflow_id, canonical phone hash)
                canonical_phone = parsed_values["phone"]
                p_hash = hash_phone(canonical_phone)
                p_masked = mask_phone(canonical_phone)

                # Reference field
                ref_field = next(f["name"] for f in schema["fields"] if f.get("reference"))
                ext_ref = str(parsed_values.get(ref_field, ""))

                # Check if lead already exists
                existing = session.query(Lead).filter_by(
                    workflow_id=workflow_id, phone_hash=p_hash
                ).first()

                if existing:
                    merged_duplicates += 1
                    # Overwrite only non-null fields
                    for k, v in parsed_values.items():
                        if v is not None:
                            if k == "preferred_language":
                                existing.preferred_language = v
                            elif k == ref_field:
                                existing.external_ref = v
                            else:
                                if existing.extra_data is None:
                                    existing.extra_data = {}
                                existing.extra_data[k] = v
                else:
                    # New lead
                    extra = {}
                    for k, v in parsed_values.items():
                        if k not in ["phone", "preferred_language", ref_field]:
                            if v is not None:
                                extra[k] = v

                    lang = parsed_values.get("preferred_language") or schema.get(
                        "fields", [{}]
                    )[-1].get("default", "en")

                    new_lead = Lead(
                        workflow_id=workflow_id,
                        phone_hash=p_hash,
                        phone_masked=p_masked,
                        external_ref=ext_ref,
                        batch_id=batch_id,
                        preferred_language=lang,
                        extra_data=extra
                    )
                    session.add(new_lead)

            session.commit()
            return (
                rows_total,
                rows_rejected,
                rejections_by_reason,
                merged_duplicates,
                field_warnings
            )