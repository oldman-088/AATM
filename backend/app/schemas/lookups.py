"""Request/response schemas for the phone-number lookup API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.phone import PHONE_INVALID_ERROR, normalize_phone_e164

PROVIDER_RESULT_STATUSES = ("success", "error", "timeout")


class LookupCreateRequest(BaseModel):
    """Create a lookup job. ``phone`` is normalized to E.164; invalid input -> 422."""

    phone: str = Field(
        min_length=1,
        max_length=64,
        description="Phone number to look up (any common formatting). Stored in E.164 form.",
    )

    @field_validator("phone", mode="before")
    @classmethod
    def _validate_phone(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(PHONE_INVALID_ERROR)
        # Validate now so bad input is rejected with 422; keep the raw string so
        # the job can record exactly what the client sent.
        normalize_phone_e164(value)
        return value

    @property
    def phone_e164(self) -> str:
        return normalize_phone_e164(self.phone)


class ProviderResultSubmitRequest(BaseModel):
    """Record one provider's raw answer for a job (worker-facing ingestion)."""

    provider: str = Field(min_length=1, max_length=80)
    status: str = Field(default="success", pattern="^(success|error|timeout)$")
    payload: dict[str, Any] = Field(default_factory=dict)
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_message: str | None = Field(default=None, max_length=2000)
    latency_ms: float | None = Field(default=None, ge=0)
    # True when no more providers will answer: the job is finalized to
    # completed (at least one success) or failed (none succeeded).
    finalize: bool = False


class ProviderResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    status: str
    http_status: int | None
    error_message: str | None
    latency_ms: float | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class LookupResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    display_name: str | None
    country: str | None
    carrier: str | None
    line_type: str | None
    spam_score: float | None
    is_spam: bool
    summary: str
    attributes: dict[str, Any]
    updated_at: datetime


class LookupJobCreated(BaseModel):
    job_id: str
    status: str
    phone_e164: str
    phone_raw: str
    created_at: datetime


class LookupJobDetail(BaseModel):
    job_id: str
    status: str
    phone_e164: str
    phone_raw: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    result: LookupResultRead | None
    provider_results: list[ProviderResultRead]
