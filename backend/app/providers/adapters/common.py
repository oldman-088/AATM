"""Shared adapter helpers: pricing, bridge limits, response parsing, endpoint checks."""

import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings
from app.providers.base import (
    BaseProviderAdapter,
)

DEFAULT_ARENA_BRIDGE_URL = "http://127.0.0.1:4100"



DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1"



DEFAULT_BRIDGE_TIMEOUT_SECONDS = 600.0



DEFAULT_BRIDGE_MAX_CONCURRENCY = 1



SAFE_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")



_MODEL_PRICING_PATH = Path(__file__).resolve().parent.parent / "model_pricing.json"



def _load_model_pricing() -> dict[str, tuple[float, float, bool]]:
    try:
        raw = json.loads(_MODEL_PRICING_PATH.read_text(encoding="utf-8"))
        models = raw.get("models") if isinstance(raw, dict) else {}
        table: dict[str, tuple[float, float, bool]] = {}
        if isinstance(models, dict):
            for name, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                in_per_1k = float(entry.get("input_per_1k_usd", 0.0) or 0.0)
                out_per_1k = float(entry.get("output_per_1k_usd", 0.0) or 0.0)
                known = bool(entry.get("pricing_known", False))
                table[str(name)] = (in_per_1k, out_per_1k, known)
        if table:
            return table
    except Exception:
        pass
    return {
        "claude-sonnet-4-5": (0.003, 0.015, True),
        "claude-opus-4-1": (0.015, 0.075, True),
        "claude-haiku-4-5": (0.0008, 0.004, True),
    }



MODEL_PRICING_CATALOG: dict[str, tuple[float, float, bool]] = _load_model_pricing()



def compute_cost_details(model: str, in_tok: int, out_tok: int) -> tuple[float, bool]:
    """Compute estimated USD cost and whether rates are known for the given model."""
    entry = MODEL_PRICING_CATALOG.get(model)
    if entry is None:
        return 0.0, False
    in_per_1k, out_per_1k, known = entry
    if not known:
        return 0.0, False
    cost = round((in_tok / 1_000.0) * in_per_1k + (out_tok / 1_000.0) * out_per_1k, 6)
    return cost, True



def compute_cost(model: str, in_tok: int, out_tok: int) -> float:
    cost, _known = compute_cost_details(model, in_tok, out_tok)
    return cost



def _configured_value(env_name: str, setting_name: str, default: Any) -> Any:
    value = os.environ.get(env_name)
    if value is not None:
        return value
    return getattr(get_settings(), setting_name, default)



def _bridge_timeout_seconds() -> float:
    try:
        value = float(_configured_value("ARENA_BRIDGE_TIMEOUT", "ARENA_BRIDGE_TIMEOUT", DEFAULT_BRIDGE_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_BRIDGE_TIMEOUT_SECONDS
    return value if math.isfinite(value) and value > 0 else DEFAULT_BRIDGE_TIMEOUT_SECONDS



def _bridge_max_concurrency() -> int:
    try:
        value = int(_configured_value("ARENA_BRIDGE_MAX_CONCURRENCY", "ARENA_BRIDGE_MAX_CONCURRENCY", DEFAULT_BRIDGE_MAX_CONCURRENCY))
    except (TypeError, ValueError):
        return DEFAULT_BRIDGE_MAX_CONCURRENCY
    return max(1, value)



_ARENA_BRIDGE_SEMAPHORE_LIMIT = _bridge_max_concurrency()



ARENA_BRIDGE_SEMAPHORE = asyncio.Semaphore(_ARENA_BRIDGE_SEMAPHORE_LIMIT)



_PROFILE_SEMAPHORES: dict[str, tuple[int, asyncio.Semaphore]] = {}



def _bridge_semaphore(profile_key: str | None = None) -> asyncio.Semaphore:
    """Per-profile bridge semaphore (Phase 5/6: one in-flight request per Arena profile/account).

    ``ARENA_BRIDGE_MAX_CONCURRENCY`` is the in-flight limit *per profile*; different employees'
    profiles run in parallel. Requests without an account/agent share the module-level semaphore.
    """
    global ARENA_BRIDGE_SEMAPHORE, _ARENA_BRIDGE_SEMAPHORE_LIMIT
    limit = _bridge_max_concurrency()
    if not profile_key:
        if limit != _ARENA_BRIDGE_SEMAPHORE_LIMIT:
            ARENA_BRIDGE_SEMAPHORE = asyncio.Semaphore(limit)
            _ARENA_BRIDGE_SEMAPHORE_LIMIT = limit
        return ARENA_BRIDGE_SEMAPHORE
    held = _PROFILE_SEMAPHORES.get(profile_key)
    if held is None or held[0] != limit:
        held = (limit, asyncio.Semaphore(limit))
        _PROFILE_SEMAPHORES[profile_key] = held
    return held[1]



def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())



def _extract_openai_completion(data: dict[str, Any], input_fallback: int) -> tuple[str, int, int, bool, bool, bool, float | None]:
    choices = data.get("choices")
    text = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            raw_content = message.get("content", "")
            if isinstance(raw_content, str):
                text = raw_content
            elif isinstance(raw_content, list):
                text = "\n".join(
                    block.get("text", "") for block in raw_content if isinstance(block, dict) and block.get("type") == "text"
                )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    input_raw = usage.get("prompt_tokens")
    output_raw = usage.get("completion_tokens")
    input_estimated = input_raw is None
    output_estimated = output_raw is None
    input_tokens = int(input_raw) if input_raw is not None else input_fallback
    output_tokens = int(output_raw) if output_raw is not None else (BaseProviderAdapter.estimate_tokens(text) if text else 0)
    confirmed_usage = input_raw is not None and output_raw is not None
    raw_cost = usage.get("cost_usd", usage.get("cost"))
    try:
        confirmed_cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        confirmed_cost = None
    if confirmed_cost is not None and (not math.isfinite(confirmed_cost) or confirmed_cost < 0):
        confirmed_cost = None
    return text, input_tokens, output_tokens, input_estimated, output_estimated, confirmed_usage, confirmed_cost



def _validate_http_endpoint(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, f"Endpoint URL must use http:// or https:// with a valid host (got scheme='{parsed.scheme or 'none'}')."
    return True, url.rstrip("/")

