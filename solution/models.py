"""
Database schema and models for Assignment A.
Defines tables for leads, the append-only interaction ledger, rejections,
and data quality tracking.
"""
from __future__ import annotations

import os
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy import (
    create_engine, Column, String, Integer, DateTime, Text, JSON,
    PrimaryKeyConstraint, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker

IST = timezone(timedelta(hours=5, minutes=30))
SALT = os.environ.get("AU_PII_SALT", "agentic_universe_salt_2026")

Base = declarative_base()


def hash_phone(canonical_phone: str) -> str:
    """
    Deterministic HMAC/SHA-256 hash of canonical phone (+91XXXXXXXXXX).
    Protects PII at rest while allowing exact O(1) matching between leads and events.
    """
    return hashlib.sha256((canonical_phone + SALT).encode("utf-8")).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """
    Masks canonical phone to '+91******' followed by the last 4 digits.
    Example: +919876543210 -> +91******3210
    """
    if len(canonical_phone) >= 4:
        return "+91******" + canonical_phone[-4:]
    return canonical_phone


class Lead(Base):
    __tablename__ = "leads"

    # Identity: (workflow_id, canonical phone hash)
    workflow_id = Column(String(64), nullable=False)
    phone_hash = Column(String(64), nullable=False)
    phone_masked = Column(String(32), nullable=False)
    external_ref = Column(String(128), nullable=False)
    batch_id = Column(String(64), nullable=False)
    preferred_language = Column(String(16), default="en")
    extra_data = Column(JSON, nullable=True)  # other fields like expiry, premium, etc.

    __table_args__ = (
        PrimaryKeyConstraint("workflow_id", "phone_hash"),
        Index("idx_lead_ref", "workflow_id", "external_ref"),
    )


class InteractionLedger(Base):
    """
    Append-only ledger of all telephony and disposition events.
    Overrides NEVER overwrite existing events.
    """
    __tablename__ = "interaction_ledger"

    event_id = Column(String(64), primary_key=True)
    event_type = Column(String(64), nullable=False)
    workflow_id = Column(String(64), nullable=False)
    call_id = Column(String(64), nullable=False, index=True)
    phone_hash = Column(String(64), nullable=True, index=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, index=True)
    payload = Column(JSON, nullable=False)

    __table_args__ = (
        Index("idx_ledger_workflow_call", "workflow_id", "call_id"),
        Index("idx_ledger_workflow_phone", "workflow_id", "phone_hash"),
    )


class Rejection(Base):
    """
    Captures rejected rows without storing PII.
    """
    __tablename__ = "rejections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_id = Column(String(64), nullable=False)
    batch_id = Column(String(64), nullable=False)
    row_number = Column(Integer, nullable=False)
    reason = Column(String(128), nullable=False)

    __table_args__ = (
        Index("idx_rejections_order", "workflow_id", "batch_id", "row_number"),
    )


class DataQualityCounter(Base):
    """
    Stores data quality stats across drops.
    """
    __tablename__ = "data_quality_counters"

    metric_name = Column(String(64), primary_key=True)
    metric_value = Column(Integer, default=0)


class IngestedFile(Base):
    """
    Guarantees true idempotency: already-processed files are skipped on re-runs.
    """
    __tablename__ = "ingested_files"

    file_path = Column(String(256), primary_key=True)
    file_hash = Column(String(64), nullable=False)
    ingested_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

def get_db_url() -> str:
    """
    Returns the database URL:
    1. AU_DATABASE_URL / DATABASE_URL environment variable if set.
    2. Default Postgres URL from docker-compose.
    """
    default_pg = "postgresql://au:au_local_only@localhost:5432/au_assignment"
    return os.environ.get("AU_DATABASE_URL", os.environ.get("DATABASE_URL", default_pg))


def get_engine(db_url: Optional[str] = None):
    url = db_url or get_db_url()
    try:
        engine = create_engine(url)
        # Test connection
        with engine.connect() as conn:
            pass
        return engine
    except Exception:
        # Graceful fallback to SQLite if Postgres is not reachable
        sqlite_url = "sqlite:///data/ledger.db"
        return create_engine(sqlite_url)


def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()