"""Phone-number lookup domain models.

* ``LookupJob``     — one lookup request for one phone number (E.164).
* ``LookupResult``  — the aggregated result of a job, merged from provider rows.
* ``ProviderResult``— one lookup provider's raw answer for a job.

The job status lifecycle is ``queued -> running -> completed | failed``:
``POST /api/v1/lookups`` creates the job as ``queued``; provider workers record
their raw answers through ``POST /api/v1/lookups/{job_id}/results``, which
recomputes the aggregated ``LookupResult`` and advances the status.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class LookupJob(Base):
    __tablename__ = "lookup_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Original client input (audit) and its canonical E.164 form (queries).
    phone_raw: Mapped[str] = mapped_column(String(64), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # queued | running | completed | failed
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LookupResult(Base):
    """Aggregated result for one job, merged from its ``ProviderResult`` rows."""

    __tablename__ = "lookup_results"
    __table_args__ = (UniqueConstraint("job_id", name="uq_lookup_results_job_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lookup_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # mobile | fixed_line | voip | toll_free | premium | unknown
    line_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spam_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..100
    is_spam: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ProviderResult(Base):
    """One lookup provider's raw answer for one job (upserted per provider)."""

    __tablename__ = "provider_results"
    __table_args__ = (UniqueConstraint("job_id", "provider", name="uq_provider_results_job_provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lookup_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # pending | success | error | timeout
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
