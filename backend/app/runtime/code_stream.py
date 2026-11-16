"""B1 — live code stream for the Command Bridge.

Every real agent write/edit, test/ruff run and pull-request review comment becomes a bounded,
redacted ``agent.code`` frame that the Bridge renders on the employee's tilted holo-screen.

Safety contract (see ``docs/design/BRIDGE_BACKEND_MAP.md`` §4):

* sensitive workspace paths (``.env``, credential stores, ``.git/``, `*.pem`, `*.key`, …) are never
  buffered and never emitted;
* every textual field passes ``redact_secrets`` and a content that still looks like it carries a
  credential is dropped instead of shown;
* a frame is capped at 400 lines / 32 KB, test output at 80 lines / 4 KB;
* the in-memory buffer keeps at most 120 frames per agent;
* frames are throttled and batched per agent (default ≤10 events/second) so 4–20 agents writing
  at once cannot flood the browser;
* only concrete artefacts are streamed — file content, diffs, the allowlisted command output,
  review comments and explicit ``done`` summaries. Model prompts/private reasoning never are.
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

from app.core.events import event_bus
from app.runtime.agent_protocol import contains_credentials, redact_secrets
from app.runtime.sandbox import is_sensitive_workspace_path

CODE_EVENT = "agent.code"

MAX_FRAME_LINES = 400
MAX_FRAME_BYTES = 32 * 1024
MAX_TEST_LINES = 80
MAX_TEST_BYTES = 4 * 1024
MAX_BUFFER_FRAMES = 120
MAX_FRAMES_PER_EVENT = 20
DEFAULT_EVENT_INTERVAL_SECONDS = 0.1
DEFAULT_MAX_EVENTS_PER_FLUSH = 10

CODE_ACTIONS = {"write", "edit", "test", "lint", "review", "done"}

# Extra guard on top of `is_sensitive_workspace_path`: a file whose *name* mentions a credential is
# never streamed, even when the runtime path policy would allow the agent to write it.
_SECRET_NAME_MARKERS = (
    "token",
    "secret",
    "credential",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "api-key",
    "private_key",
    "private-key",
    "id_rsa",
    "id_ed25519",
)

# `redact_secrets` only matches whole words (`password`, `token`, …); identifier forms such as
# `DB_PASSWORD` or `GITHUB_TOKEN` slip through it, so the stream applies the same rule with an
# identifier-aware boundary before anything leaves the process.
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

_STREAM_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([A-Za-z0-9_.\-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[A-Za-z0-9_.\-]*[ \t]*[:=][ \t]*)"
    r"(['\"]?)[^\s'\",;)]{4,}\2"
)

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".md": "markdown",
    ".css": "css",
    ".scss": "scss",
    ".html": "html",
    ".vue": "vue",
    ".sh": "bash",
    ".bash": "bash",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
}


def is_streamable_path(path: str) -> bool:
    """True only for workspace paths the Bridge may display."""
    if not path or is_sensitive_workspace_path(path):
        return False
    basename = path.rsplit("/", 1)[-1].lower()
    return not any(marker in basename for marker in _SECRET_NAME_MARKERS)


def redact_frame_text(text: str) -> str:
    """Redact secrets with both the shared rule and the identifier-aware stream rule."""
    return _STREAM_SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redact_secrets(text or ""))


def still_looks_secret(text: str) -> bool:
    """True when a (redacted) frame body still looks like it carries a credential.

    Our own ``[REDACTED]`` marker is removed before the second check, otherwise every redacted
    assignment would keep matching and the frame would be dropped instead of shown.
    """
    cleaned = (text or "").replace("[REDACTED]", "x")
    return bool(contains_credentials(cleaned) or _STREAM_SECRET_ASSIGNMENT.search(cleaned))


def language_for(path: str) -> str:
    """Best-effort language tag from a file name; unknown extensions are ``text``."""
    lowered = (path or "").lower()
    for suffix, language in _LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return "text"


def _clip(text: str, *, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """Bound text by line count and byte size; returns (text, truncated)."""
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    clipped = "\n".join(lines)
    encoded = clipped.encode("utf-8")
    if len(encoded) > max_bytes:
        clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True
    return clipped, truncated


def _safe_text(raw: str, *, max_lines: int, max_bytes: int) -> tuple[str, bool, bool]:
    """Redact, then clip. Returns (text, truncated, dropped)."""
    if _PRIVATE_KEY_BLOCK.search(raw or ""):
        return "", False, True
    redacted = redact_frame_text(raw or "")
    if still_looks_secret(redacted):
        return "", False, True
    text, truncated = _clip(redacted, max_lines=max_lines, max_bytes=max_bytes)
    return text, truncated, False


def diff_lines(previous: str, current: str, *, context: int = 2, max_lines: int = 240) -> list[dict[str, str]]:
    """Compact add/del/context hunks between two versions of the same file (bounded)."""
    old_lines = (previous or "").splitlines()
    new_lines = (current or "").splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    out: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            shared = old_lines[i1:i2]
            if not shared:
                continue
            # Keep a couple of context lines around a change, not the whole untouched file.
            head = shared[: context + 1]
            tail = shared[-(1):] if len(shared) > context + 2 else []
            for line in head:
                out.append({"type": "ctx", "text": line})
            if tail:
                out.append({"type": "gap", "text": f"… {len(shared) - len(head) - len(tail)} unchanged line(s) …"})
                for line in tail:
                    out.append({"type": "ctx", "text": line})
            continue
        for line in old_lines[i1:i2]:
            out.append({"type": "del", "text": line})
        for line in new_lines[j1:j2]:
            out.append({"type": "add", "text": line})
        if len(out) > max_lines:
            out = out[:max_lines]
            out.append({"type": "gap", "text": "… diff truncated …"})
            break
    return out


def _base_frame(payload: dict[str, Any]) -> dict[str, Any] | None:
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        return None
    frame: dict[str, Any] = {
        "agent_id": agent_id,
        "task_id": (str(payload["task_id"]) if payload.get("task_id") else None),
        "action": str(payload.get("action") or "write"),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if frame["action"] not in CODE_ACTIONS:
        frame["action"] = "write"
    return frame


def build_write_frame(
    *,
    agent_id: str,
    task_id: str | None,
    path: str,
    content: str,
    previous_content: str | None = None,
    action: str = "write",
) -> dict[str, Any] | None:
    """Frame for a real workspace write/edit. Returns ``None`` when nothing may be streamed."""
    if not is_streamable_path(path):
        return None
    text, truncated, dropped = _safe_text(content, max_lines=MAX_FRAME_LINES, max_bytes=MAX_FRAME_BYTES)
    if dropped:
        return None
    frame = _base_frame({"agent_id": agent_id, "task_id": task_id, "action": action})
    if frame is None:
        return None
    frame.update(
        {
            "path": path,
            "language": language_for(path),
            "content": text,
            "lines": len(text.splitlines()),
            "truncated": truncated,
        }
    )
    if previous_content is not None:
        diff = diff_lines(previous_content, content)
        if diff:
            frame["diff"] = diff
    return frame


def build_test_frame(
    *,
    agent_id: str,
    task_id: str | None,
    command: Iterable[str] | str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> dict[str, Any] | None:
    """Frame for a pytest/ruff run: the allowlisted command plus its bounded output."""
    command_text = command if isinstance(command, str) else " ".join(str(part) for part in command)
    parts = command_text.split()
    action = "lint" if parts and parts[0] == "ruff" else "test"
    text, truncated, dropped = _safe_text(
        "\n".join(part for part in (stdout, stderr) if part),
        max_lines=MAX_TEST_LINES,
        max_bytes=MAX_TEST_BYTES,
    )
    if dropped:
        text, truncated = "[redacted output]", True
    frame = _base_frame({"agent_id": agent_id, "task_id": task_id, "action": action})
    if frame is None:
        return None
    frame.update(
        {
            "path": None,
            "language": "text",
            "command": redact_frame_text(command_text)[:400],
            "output_lines": text.splitlines(),
            "exit_code": int(exit_code),
            "ok": int(exit_code) == 0,
            "truncated": truncated,
        }
    )
    return frame


def build_review_frame(*, agent_id: str, task_id: str | None, body: str, event: str) -> dict[str, Any] | None:
    """Frame for a pull-request review comment posted by a reviewer employee."""
    text, truncated, dropped = _safe_text(body, max_lines=MAX_TEST_LINES, max_bytes=MAX_TEST_BYTES)
    if dropped:
        return None
    frame = _base_frame({"agent_id": agent_id, "task_id": task_id, "action": "review"})
    if frame is None:
        return None
    frame.update(
        {
            "path": None,
            "language": "text",
            "review_event": str(event or "COMMENT"),
            "output_lines": text.splitlines(),
            "truncated": truncated,
        }
    )
    return frame


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(.*)$")


def parse_unified_patch(patch: str, *, max_lines: int = 240) -> list[dict[str, str]]:
    """GitHub compare ``patch`` (unified diff hunks) -> the same add/del/ctx/gap lines as ``diff_lines``."""
    out: list[dict[str, str]] = []
    for line in (patch or "").splitlines():
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        hunk = _HUNK_RE.match(line)
        if hunk:
            out.append({"type": "gap", "text": f"@@{hunk.group(1)}".rstrip() if hunk.group(1).strip() else "@@"})
        elif line.startswith("+"):
            out.append({"type": "add", "text": line[1:]})
        elif line.startswith("-"):
            out.append({"type": "del", "text": line[1:]})
        else:
            out.append({"type": "ctx", "text": line[1:] if line.startswith(" ") else line})
        if len(out) >= max_lines:
            out.append({"type": "gap", "text": "… diff truncated …"})
            break
    return out


def build_github_frame(
    *,
    agent_id: str,
    task_id: str | None,
    branch: str,
    filename: str,
    patch: str,
    file_status: str = "modified",
    additions: int = 0,
    deletions: int = 0,
    head_sha: str | None = None,
    compare_url: str | None = None,
) -> dict[str, Any] | None:
    """Frame for a file an Arena Agent changed on its GitHub branch (from the compare API).

    Same safety contract as workspace writes: sensitive paths are never streamed, the patch is
    redacted and clipped, and a patch that still looks like it carries a credential is dropped.
    """
    if not is_streamable_path(filename):
        return None
    text, truncated, dropped = _safe_text(patch or "", max_lines=MAX_FRAME_LINES, max_bytes=MAX_FRAME_BYTES)
    if dropped:
        return None
    frame = _base_frame({"agent_id": agent_id, "task_id": task_id, "action": "edit"})
    if frame is None:
        return None
    diff = parse_unified_patch(text)
    frame.update(
        {
            "source": "arena_github",
            "path": filename,
            "language": language_for(filename),
            "content": "",
            "diff": diff,
            "lines": len(diff),
            "truncated": truncated,
            "branch": branch,
            "file_status": file_status,
            "additions": int(additions),
            "deletions": int(deletions),
            "commit": (head_sha or "")[:40] or None,
            "compare_url": compare_url,
        }
    )
    return frame


class CodeStream:
    """Per-agent bounded buffer plus a throttled/batched publisher."""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_EVENT_INTERVAL_SECONDS,
        max_events_per_flush: int = DEFAULT_MAX_EVENTS_PER_FLUSH,
        buffer_frames: int = MAX_BUFFER_FRAMES,
        clock=time.monotonic,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.max_events_per_flush = max_events_per_flush
        self.buffer_frames = buffer_frames
        self._clock = clock
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._last_emit: dict[str, float] = {}
        self._flusher: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ buffer
    def record(self, frame: dict[str, Any] | None) -> dict[str, Any] | None:
        """Buffer one frame (already sanitised) and queue it for the next batched emit."""
        if not frame or not frame.get("agent_id"):
            return None
        agent_id = frame["agent_id"]
        self._buffers.setdefault(agent_id, deque(maxlen=self.buffer_frames)).append(frame)
        self._pending.setdefault(agent_id, []).append(frame)
        return frame

    def buffer(self, agent_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent frames for an agent, oldest first."""
        frames = list(self._buffers.get(agent_id, ()))
        capped = max(1, min(int(limit or 40), self.buffer_frames))
        return frames[-capped:]

    def pending_count(self, agent_id: str | None = None) -> int:
        if agent_id is not None:
            return len(self._pending.get(agent_id, ()))
        return sum(len(items) for items in self._pending.values())

    def clear(self, agent_id: str | None = None) -> None:
        """Drop buffered/pending frames (tests and agent reset)."""
        if agent_id is None:
            self._buffers.clear()
            self._pending.clear()
            self._last_emit.clear()
            return
        self._buffers.pop(agent_id, None)
        self._pending.pop(agent_id, None)
        self._last_emit.pop(agent_id, None)

    # ------------------------------------------------------------------ throttling
    def _due_agents(self, now: float) -> list[str]:
        due = [
            agent_id
            for agent_id, frames in self._pending.items()
            if frames and now - self._last_emit.get(agent_id, -1e9) >= self.interval_seconds
        ]
        # Fair round-robin: the agent that has been waiting longest goes first.
        due.sort(key=lambda agent_id: self._last_emit.get(agent_id, -1e9))
        return due

    def take_due_events(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Pop the batched events that are allowed to be emitted right now."""
        moment = self._clock() if now is None else now
        events: list[dict[str, Any]] = []
        for agent_id in self._due_agents(moment)[: self.max_events_per_flush]:
            frames = self._pending.get(agent_id) or []
            if not frames:
                self._pending.pop(agent_id, None)
                continue
            batch = frames[:MAX_FRAMES_PER_EVENT]
            remainder = frames[MAX_FRAMES_PER_EVENT:]
            if remainder:
                self._pending[agent_id] = remainder
            else:
                self._pending.pop(agent_id, None)
            self._last_emit[agent_id] = moment
            last = batch[-1]
            events.append(
                {
                    "agent_id": agent_id,
                    "task_id": last.get("task_id"),
                    "path": last.get("path"),
                    "language": last.get("language"),
                    "action": last.get("action"),
                    "count": len(batch),
                    "frames": batch,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return events

    def next_flush_delay(self, *, now: float | None = None) -> float | None:
        """Seconds until the next pending frame may be emitted, or ``None`` when idle."""
        pending_agents = [agent_id for agent_id, frames in self._pending.items() if frames]
        if not pending_agents:
            return None
        moment = self._clock() if now is None else now
        wait = min(self.interval_seconds - (moment - self._last_emit.get(agent_id, -1e9)) for agent_id in pending_agents)
        return max(0.0, wait)

    async def publish(self, frame: dict[str, Any] | None) -> None:
        """Record a frame, emit what is due now, and schedule the rest of the batch."""
        if self.record(frame) is None:
            return
        for event in self.take_due_events():
            await event_bus.emit(CODE_EVENT, event)
        if self.pending_count():
            self._ensure_flusher()

    def _ensure_flusher(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # no loop (sync caller): frames stay buffered for the endpoint
            return
        if self._flusher is not None and not self._flusher.done():
            return
        self._flusher = running.create_task(self._flush_loop(), name="aatm-code-stream-flusher")

    async def _flush_loop(self) -> None:
        try:
            while True:
                delay = self.next_flush_delay()
                if delay is None:
                    return
                if delay > 0:
                    await asyncio.sleep(delay)
                for event in self.take_due_events():
                    await event_bus.emit(CODE_EVENT, event)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        finally:
            self._flusher = None

    async def drain(self) -> list[dict[str, Any]]:
        """Force every pending frame out (used by tests and shutdown)."""
        if self._flusher is not None and not self._flusher.done():
            self._flusher.cancel()
            await asyncio.gather(self._flusher, return_exceptions=True)
        events: list[dict[str, Any]] = []
        while True:
            batch = self.take_due_events(now=self._clock() + self.interval_seconds + 1.0)
            if not batch:
                break
            events.extend(batch)
        for event in events:
            await event_bus.emit(CODE_EVENT, event)
        return events


# Process-wide stream. The Bridge is a live view, so the buffer is intentionally in-memory.
code_stream = CodeStream()


__all__ = [
    "CODE_EVENT",
    "CODE_ACTIONS",
    "CodeStream",
    "MAX_FRAME_BYTES",
    "MAX_FRAME_LINES",
    "build_github_frame",
    "build_review_frame",
    "build_test_frame",
    "build_write_frame",
    "code_stream",
    "diff_lines",
    "is_streamable_path",
    "language_for",
    "parse_unified_patch",
    "redact_frame_text",
    "still_looks_secret",
]
