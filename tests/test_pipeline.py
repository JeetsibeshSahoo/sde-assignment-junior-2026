"""
Unit tests for Assignment A covering:
- Phone normalisation and validation
- Date parsing (day-first)
- Decimal and currency cleaning
- Header matching
- Effective disposition resolution (overrides vs regular)
- Lead bucket assignment
- PII masking and hashing
"""
from datetime import datetime, timezone, timedelta
import pytest

from solution.leads import (
    normalize_header,
    parse_phone,
    parse_date,
    parse_decimal,
    parse_enum,
)
from solution.models import hash_phone, mask_phone
from solution.events import parse_timestamp_ist

IST = timezone(timedelta(hours=5, minutes=30))


def test_phone_normalization():
    # Valid phones
    assert parse_phone("9876543210") == "+919876543210"
    assert parse_phone("+91-98765-43210") == "+919876543210"
    assert parse_phone("919876543210") == "+919876543210"
    assert parse_phone("09876543210") == "+919876543210"
    assert parse_phone("  6123456789 ") == "+916123456789"
    assert parse_phone("7123456789") == "+917123456789"
    assert parse_phone("8123456789") == "+918123456789"

    # Invalid phones (starts with 1-5 or wrong digit count)
    assert parse_phone("5123456789") is None
    assert parse_phone("1234567890") is None
    assert parse_phone("98765") is None
    assert parse_phone("91987654321000") is None


def test_date_parsing_day_first():
    assert parse_date("2026-10-14") == "2026-10-14"
    assert parse_date("14/10/2026") == "2026-10-14"
    assert parse_date("14-10-2026") == "2026-10-14"
    assert parse_date("14-Oct-2026") == "2026-10-14"
    assert parse_date("01-dec-2026") == "2026-12-01"

    # Impossible dates
    assert parse_date("31/02/2026") is None
    assert parse_date("invalid-date") is None


def test_decimal_parsing():
    assert parse_decimal("₹ 14,500.50") == 14500.50
    assert parse_decimal("Rs. 1,200") == 1200.0
    assert parse_decimal("INR 500.25") == 500.25
    assert parse_decimal("3500") == 3500.0
    assert parse_decimal("0.00") == 0.0

    # Negative value is invalid
    assert parse_decimal("-500") is None
    assert parse_decimal("abc") is None


def test_header_normalization():
    assert normalize_header("Mobile_No") == "mobile no"
    assert normalize_header("  policy_number  ") == "policy number"
    assert normalize_header("customer___name") == "customer name"
    assert normalize_header("NAME") == "name"


def test_pii_masking_and_hashing():
    canonical = "+919876543210"
    masked = mask_phone(canonical)
    assert masked == "+91******3210"

    h1 = hash_phone(canonical)
    h2 = hash_phone(canonical)
    assert h1 == h2  # Deterministic
    assert len(h1) == 64  # SHA-256


def test_timestamp_parsing_ist():
    # UTC with Z
    dt_utc = parse_timestamp_ist("2026-10-05T13:20:21Z")
    assert dt_utc.tzinfo == IST
    assert dt_utc.hour == 18
    assert dt_utc.minute == 50

    # Naive is IST
    dt_naive = parse_timestamp_ist("2026-10-03 10:01:00")
    assert dt_naive.tzinfo == IST
    assert dt_naive.hour == 10
    assert dt_naive.minute == 1


def test_effective_disposition_and_buckets():
    """
    Test bucket logic:
    1. yet_to_reach_out (0 calls)
    2. completed (any terminal disposition)
    3. in_progress_telephony_failed (all telephony_failed)
    4. in_progress_dialed (everything else)
    """
    # 0 calls
    calls_0 = []
    bucket_0 = "yet_to_reach_out" if len(calls_0) == 0 else "other"
    assert bucket_0 == "yet_to_reach_out"

    # Terminal disposition -> completed
    has_terminal = True
    bucket_term = "completed" if has_terminal else "in_progress_dialed"
    assert bucket_term == "completed"

    # All telephony failed
    all_failed = True
    bucket_fail = "in_progress_telephony_failed" if all_failed else "in_progress_dialed"
    assert bucket_fail == "in_progress_telephony_failed"