"""B3 — demo mode for the Command Bridge.

The bridge is only meaningful when agents are writing code. So the owner can show it without a
model key, a repository or a running crew, `DEMO_MODE` streams a *scripted, clearly labelled* code
story through a read-only endpoint:

* it never writes to the database, never calls a model and never touches an agent workspace;
* every frame carries `demo: true` and the API reports `demo_mode: true` in `/api/health`, so the UI
  can badge it — demo data can never be mistaken for a real run;
* it is refused in `ENVIRONMENT=production` and it is off by default;
* when it is on, the bridge renders the demo feed *instead of* the live `agent.code` stream, so real
  and demo frames are never mixed.

The frames are generated from a fixed script keyed by a time bucket, which makes the endpoint
deterministic and testable (no background task, no timers to leak between tests).
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import get_settings

DEMO_TICK_SECONDS = 3


def demo_mode_active() -> bool:
    """`DEMO_MODE` is honoured everywhere except production, where it is always off."""
    settings = get_settings()
    if not bool(getattr(settings, "DEMO_MODE", False)):
        return False
    return (settings.ENVIRONMENT or "").strip().lower() != "production"


def demo_disabled_reason() -> str:
    settings = get_settings()
    if bool(getattr(settings, "DEMO_MODE", False)) and (settings.ENVIRONMENT or "").lower() == "production":
        return "DEMO_MODE is refused in production"
    return "DEMO_MODE is off (set DEMO_MODE=true in .env to show the bridge without a model)"


# --- the scripted story ------------------------------------------------------------------------
# One step per tick, per employee. The code is intentionally small and readable: it is a stage
# prop, and every frame is flagged `demo` so nobody can mistake it for a real agent's work.

_WRITE = """def stream_frame(agent_id: str, path: str, content: str) -> dict:
    \"\"\"A bounded, redacted code frame for one employee.\"\"\"
    if not is_streamable_path(path):
        return None
    lines = content.splitlines()[:MAX_FRAME_LINES]
    return {
        "agent_id": agent_id,
        "path": path,
        "lines": len(lines),
        "truncated": len(content.splitlines()) > MAX_FRAME_LINES,
    }
"""

_TEST = """import pytest

from app.runtime.code_stream import build_write_frame, is_streamable_path


def test_env_file_is_never_streamed():
    assert build_write_frame(agent_id="worker-1", task_id=None, path=".env", content="X=1") is None


def test_write_frame_keeps_the_head_of_a_large_file():
    frame = build_write_frame(agent_id="worker-1", task_id=None, path="app/mod.py", content="x\\n" * 500)
    assert frame["truncated"] is True
"""

_REVIEW = """Two things before merge:

* the frame cap is enforced before the buffer, not after — good;
* please assert the diff hunks survive redaction (see the review test).
"""

DONE = "Done: the live code stream is wired to the bridge screen."


def _steps(agent_id: str, index: int) -> list[dict[str, Any]]:
    """The four-frame story every employee plays, offset per seat so the bridge looks alive."""
    rotation = [
        {
            "action": "write",
            "path": "backend/app/runtime/code_stream.py",
            "language": "python",
            "content": _WRITE,
        },
        {
            "action": "test",
            "path": None,
            "command": "pytest -q tests/test_b1_code_stream.py",
            "exit_code": 0,
            "ok": True,
            "output_lines": ["13 passed, 1 warning in 3.41s"],
        },
        {
            "action": "review",
            "path": "backend/app/runtime/code_stream.py",
            "review_event": "COMMENT",
            "output_lines": _REVIEW.strip().splitlines(),
        },
        {"action": "done", "path": None, "lines": [DONE]},
    ]
    return rotation[index % len(rotation)]


def demo_frames(agent_ids: list[str], limit: int = 12, tick: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Scripted frames per agent for the current tick (oldest first)."""
    bucket = int(time.time() // DEMO_TICK_SECONDS) if tick is None else tick
    out: dict[str, list[dict[str, Any]]] = {}
    for index, agent_id in enumerate(agent_ids):
        step = _steps(agent_id, bucket + index)
        frame: dict[str, Any] = {
            "agent_id": agent_id,
            "task_id": None,
            "demo": True,
            "action": step["action"],
            "path": step.get("path"),
            "language": step.get("language", "text"),
        }
        for key in ("content", "command", "exit_code", "ok", "output_lines", "review_event", "lines"):
            if key in step:
                frame[key] = step[key]
        # `limit` frames at most, so the payload matches the live endpoint's shape.
        out[agent_id] = [frame][-max(1, min(limit, 12)):]
    return out
