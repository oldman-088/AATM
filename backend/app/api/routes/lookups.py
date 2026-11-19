"""Phone-number lookup API.

* ``POST /api/v1/lookups``                   — create a lookup job for one phone
  number; the number is normalized to E.164 (invalid input -> 422) and the job
  is returned with status ``queued``.
* ``GET /api/v1/lookups/{job_id}``           — job status, the aggregated result
  and every per-provider raw result.
* ``POST /api/v1/lookups/{job_id}/results``  — record one provider's raw answer
  (worker-facing ingestion, idempotent per provider). Each submission
  recomputes the aggregated result; ``finalize=True`` closes the job as
  ``completed`` (at least one provider succeeded) or ``failed``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.base import utcnow
from app.db.session import get_db
from app.models import LookupJob, LookupResult, ProviderResult, User
from app.schemas.lookups import (
    LookupCreateRequest,
    LookupJobCreated,
    LookupJobDetail,
    LookupResultRead,
    ProviderResultRead,
    ProviderResultSubmitRequest,
)

router = APIRouter(tags=["lookups"], dependencies=[Depends(get_current_user)])

# Payload keys merged into the aggregated result fields; everything else lands in
# ``LookupResult.attributes`` under the provider name.
_AGGREGATED_PAYLOAD_KEYS = frozenset({"display_name", "name", "carrier", "country", "line_type", "spam_score", "is_spam"})


def _first_non_empty(rows: list[ProviderResult], getter) -> str | None:
    for row in rows:
        value = getter(row)
        if isinstance(value, str):
            value = value.strip()
        if value:
            return value
    return None


def _payload(row: ProviderResult) -> dict[str, Any]:
    return row.payload or {}


def _spam_score_of(row: ProviderResult) -> float | None:
    raw = _payload(row).get("spam_score")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0.0, min(100.0, float(raw)))


def aggregate_provider_results(rows: list[ProviderResult]) -> dict[str, Any]:
    """Merge per-provider raw results into one aggregated result document.

    Only rows with ``status == "success"`` contribute values. Field values come
    from the earliest successful provider that reports them; ``spam_score`` is
    the maximum reported score; ``is_spam`` is true when any provider flags the
    number. Unmapped payload keys are preserved per provider in ``attributes``.
    """
    ordered = sorted(rows, key=lambda r: (r.created_at.isoformat() if r.created_at else "", r.provider))
    successful = [r for r in ordered if r.status == "success"]

    display_name = _first_non_empty(successful, lambda r: _payload(r).get("display_name") or _payload(r).get("name"))
    country = _first_non_empty(successful, lambda r: _payload(r).get("country"))
    carrier = _first_non_empty(successful, lambda r: _payload(r).get("carrier"))
    line_type = _first_non_empty(successful, lambda r: _payload(r).get("line_type"))
    scores = [score for score in (_spam_score_of(r) for r in successful) if score is not None]
    spam_score = max(scores) if scores else None
    is_spam = any(bool(_payload(r).get("is_spam")) for r in successful)

    summary = " · ".join(str(part) for part in (display_name, carrier, country, line_type) if part)
    attributes = {
        r.provider: {k: v for k, v in _payload(r).items() if k not in _AGGREGATED_PAYLOAD_KEYS}
        for r in successful
    }
    return {
        "display_name": display_name,
        "country": country,
        "carrier": carrier,
        "line_type": line_type,
        "spam_score": spam_score,
        "is_spam": is_spam,
        "summary": summary,
        "attributes": attributes,
    }


async def _load_provider_rows(db: AsyncSession, job_id: str) -> list[ProviderResult]:
    res = await db.execute(select(ProviderResult).where(ProviderResult.job_id == job_id))
    return sorted(res.scalars().all(), key=lambda r: (r.created_at.isoformat() if r.created_at else "", r.provider))


async def _load_result_row(db: AsyncSession, job_id: str) -> LookupResult | None:
    res = await db.execute(select(LookupResult).where(LookupResult.job_id == job_id))
    return res.scalar_one_or_none()


async def _get_job_or_404(db: AsyncSession, job_id: str) -> LookupJob:
    res = await db.execute(select(LookupJob).where(LookupJob.id == job_id))
    job = res.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="lookup job not found")
    return job


def _job_detail(job: LookupJob, result_row: LookupResult | None, provider_rows: list[ProviderResult]) -> LookupJobDetail:
    return LookupJobDetail(
        job_id=job.id,
        status=job.status,
        phone_e164=job.phone_e164,
        phone_raw=job.phone_raw,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_message=job.error_message,
        result=LookupResultRead.model_validate(result_row) if result_row else None,
        provider_results=[ProviderResultRead.model_validate(row) for row in provider_rows],
    )


@router.post("/lookups", status_code=201, response_model=LookupJobCreated)
async def create_lookup(
    request: LookupCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LookupJobCreated:
    job = LookupJob(
        phone_raw=request.phone,
        phone_e164=request.phone_e164,
        status="queued",
        created_by=user.email,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return LookupJobCreated(
        job_id=job.id,
        status=job.status,
        phone_e164=job.phone_e164,
        phone_raw=job.phone_raw,
        created_at=job.created_at,
    )


@router.get("/lookups/{job_id}", response_model=LookupJobDetail)
async def get_lookup(job_id: str, db: AsyncSession = Depends(get_db)) -> LookupJobDetail:
    job = await _get_job_or_404(db, job_id)
    result_row = await _load_result_row(db, job_id)
    provider_rows = await _load_provider_rows(db, job_id)
    return _job_detail(job, result_row, provider_rows)


@router.post("/lookups/{job_id}/results", response_model=LookupJobDetail)
async def submit_provider_result(
    job_id: str,
    request: ProviderResultSubmitRequest,
    db: AsyncSession = Depends(get_db),
) -> LookupJobDetail:
    job = await _get_job_or_404(db, job_id)

    res = await db.execute(
        select(ProviderResult).where(ProviderResult.job_id == job_id, ProviderResult.provider == request.provider)
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = ProviderResult(job_id=job_id, provider=request.provider)
        db.add(row)
    row.status = request.status
    row.payload = request.payload
    row.http_status = request.http_status
    row.error_message = request.error_message
    row.latency_ms = request.latency_ms
    await db.flush()  # session autoflush is off; persist before re-reading the rows

    provider_rows = await _load_provider_rows(db, job_id)
    aggregate = aggregate_provider_results(provider_rows)
    result_row = await _load_result_row(db, job_id)
    if result_row is None:
        result_row = LookupResult(job_id=job_id)
        db.add(result_row)
    for field in ("display_name", "country", "carrier", "line_type", "spam_score", "is_spam", "summary", "attributes"):
        setattr(result_row, field, aggregate[field])

    now = utcnow()
    job.updated_at = now
    if request.finalize:
        if any(r.status == "success" for r in provider_rows):
            job.status = "completed"
            job.error_message = None
        else:
            job.status = "failed"
            job.error_message = "every provider result failed or timed out"
        job.completed_at = now
    elif job.status == "queued":
        job.status = "running"
        job.started_at = now

    await db.commit()
    await db.refresh(job)
    result_row = await _load_result_row(db, job_id)
    provider_rows = await _load_provider_rows(db, job_id)
    return _job_detail(job, result_row, provider_rows)
