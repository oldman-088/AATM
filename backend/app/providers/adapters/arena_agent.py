"""Arena Agent as an employee execution engine.

Arena Agent is the web coding agent that works directly on GitHub: it creates its own
``arena/<id>`` branch, commits, pushes and opens the pull request. AATM drives one Arena Agent
session per employee through that employee's own Chrome profile (Tampermonkey userscript <-> local
bridge). This adapter is the engine API the orchestrator (``app.orchestration.arena_runner``) uses:

* ``new_session`` / ``submit`` / ``keep_working`` / ``read_last_reply`` / ``selector_test``;
* ``state`` / ``wait_for_state`` over the page states reported by the userscript;
* ``generate`` runs one prompt as a complete session cycle (submit -> continue while Keep working is
  shown, within ``ARENA_AGENT_MAX_KEEP_WORKING`` where 0 = unlimited -> idle -> last reply).

Pull requests and code are never read from the Arena page: the orchestrator uses the GitHub API.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import get_settings
from app.providers.base import (
    BaseProviderAdapter,
    ProviderHealth,
    ProviderRequest,
    ProviderResponse,
)

COMMAND_TIMEOUTS = {"new_session": 90.0, "select_repo": 40.0, "submit": 60.0, "keep_working": 30.0, "read_last_reply": 20.0, "selector_test": 20.0}
QUIET_STATES = {"idle", "ready"}
MAX_REPLY_CHARS = 60_000


class ArenaSubmitQueue:
    """The single submit path for every prompt AATM sends to an Arena page (workers and manager).

    Each profile has one FIFO slot, so one prompt is being sent per browser tab at a time, and every
    submission is counted per source. Any limit added later (rate, quota, pause) belongs here so it
    applies to the manager and the workers alike.
    """

    def __init__(self, history: int = 500) -> None:
        self._locks: dict[str, tuple[Any, asyncio.Lock]] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self.in_flight: dict[str, str] = {}
        self.max_in_flight: dict[str, int] = {}
        self._active: dict[str, int] = {}

    def _lock(self, profile_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        current = self._locks.get(profile_id)
        if current is None or current[0] is not loop:  # locks never cross event loops (tests, restarts)
            current = (loop, asyncio.Lock())
            self._locks[profile_id] = current
        return current[1]

    @asynccontextmanager
    async def slot(self, profile_id: str, *, source: str) -> AsyncIterator[None]:
        async with self._lock(profile_id):
            self._active[profile_id] = self._active.get(profile_id, 0) + 1
            self.max_in_flight[profile_id] = max(self.max_in_flight.get(profile_id, 0), self._active[profile_id])
            self.in_flight[profile_id] = source
            self.history.append({"profile_id": profile_id, "source": source, "at": time.time()})
            try:
                yield
            finally:
                self._active[profile_id] -= 1
                self.in_flight.pop(profile_id, None)

    def sources(self) -> list[str]:
        return [entry["source"] for entry in self.history]


arena_submit_queue = ArenaSubmitQueue()


class ArenaAgentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code or "arena_agent_error"
        self.message = message


TOOL_LIMIT_CONTINUE = "continue"  # Arena's tool-call limit pause resumes only on a new message


def keep_working_allowed(count: int) -> bool:
    """``ARENA_AGENT_MAX_KEEP_WORKING`` = 0 means unlimited continuation."""
    limit = get_settings().ARENA_AGENT_MAX_KEEP_WORKING
    return limit == 0 or count < limit


def redact_reply(text: str) -> dict[str, Any]:
    from app.runtime.agent_protocol import redact_secrets

    raw = str(text or "")
    clean = redact_secrets(raw)
    return {"text": clean[:MAX_REPLY_CHARS], "truncated": len(clean) > MAX_REPLY_CHARS, "chars": len(raw)}


class ArenaAgentAdapter(BaseProviderAdapter):
    provider_type = "arena_agent"
    repo_select_attempts = 8
    repo_select_wait_seconds = 2.5

    def __init__(self, provider_id: str = "arena-agent", endpoint_url: str | None = None,
                 credential_ref: str | None = "env:ARENA_BRIDGE_TOKEN", default_model: str = "arena-agent-coder") -> None:
        super().__init__(provider_id, endpoint_url, credential_ref, default_model)

    # ------------------------------------------------------------------ engine API
    async def command(self, profile_id: str, command: str, text: str | None = None, *, timeout: float | None = None,
                      repo: str | None = None, branch: str | None = None, url: str | None = None) -> dict[str, Any]:
        """Run one validated command on the profile's Arena page; raises ArenaAgentError on failure."""
        from app.browser.arena_connector import bridge_call

        wait = timeout or COMMAND_TIMEOUTS.get(command, 60.0)
        args: dict[str, Any] = {"text": text} if text is not None else {}
        if repo:
            args["repo"] = repo
        if branch:
            args["branch"] = branch
        if url:
            args["url"] = url
        res = await bridge_call("POST", f"/v1/connector/profiles/{profile_id}/command",
                                {"command": command, "args": args, "timeout_seconds": wait}, timeout=wait + 15)
        if not res.get("ok"):
            raise ArenaAgentError(str(res.get("code") or "bridge_error"), str(res.get("message") or "Arena command failed."))
        result = dict(res.get("result") or {})
        if command == "read_last_reply":
            result.update(redact_reply(result.get("text") or ""))
        return result

    async def state(self, profile_id: str) -> dict[str, Any]:
        from app.browser.arena_sessions import bridge_profile_state

        return await bridge_profile_state(profile_id)

    async def wait_for_state(self, profile_id: str, wanted: set[str], *, timeout: float, poll: float = 0.5) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await self.state(profile_id)
            current = last.get("agent_state")
            if last.get("status") == "needs_human" or current == "needs_human":
                raise ArenaAgentError("needs_human", str(last.get("needs_human_reason") or "Arena page needs a human."))
            if current in wanted and last.get("connected"):
                return last
            await asyncio.sleep(poll)
        raise ArenaAgentError("timeout", f"Arena page did not reach {sorted(wanted)} (last: {last.get('agent_state')}).")

    async def new_session(self, profile_id: str, *, ready_timeout: float = 90.0) -> dict[str, Any]:
        result = await self.command(profile_id, "new_session")
        if result.get("navigating"):
            await asyncio.sleep(1.0)  # let the old page unload before waiting for the new one
        await self.wait_for_state(profile_id, {"ready"}, timeout=ready_timeout)
        return result

    async def open_chat(self, profile_id: str, url: str) -> dict[str, Any]:
        """Load an existing chat in this window (same-origin navigation; the bridge checks the origin)."""
        return await self.command(profile_id, "new_session", url=url)

    async def select_repo(self, profile_id: str, repo: str) -> dict[str, Any]:
        """Select the task's repository for this chat; raises unless the page confirms it.

        A freshly loaded page shows the composer before the repository picker, so a miss is retried."""
        result: dict[str, Any] = {}
        for attempt in range(self.repo_select_attempts):
            if attempt:
                await asyncio.sleep(self.repo_select_wait_seconds)
            result = await self.command(profile_id, "select_repo", repo=repo)
            if result.get("selected"):
                return result
        detail = result.get("current") or "unknown"
        raise ArenaAgentError(str(result.get("code") or "repo_not_selected"),
                              f"Could not select {repo} in Arena (selected: {detail}).")

    async def submit(self, profile_id: str, text: str, *, source: str = "worker", repo: str | None = None) -> dict[str, Any]:
        """Send a prompt. With ``repo`` the page refuses it unless that repository is selected."""
        async with arena_submit_queue.slot(profile_id, source=source):
            result = await (self.command(profile_id, "submit", text, repo=repo) if repo else self.command(profile_id, "submit", text))
        if not result.get("sent"):
            raise ArenaAgentError("send_failed", "The Arena page did not confirm the message was sent.")
        return result

    async def keep_working(self, profile_id: str) -> dict[str, Any]:
        return await self.command(profile_id, "keep_working")

    async def transcript_mark(self, profile_id: str) -> dict[str, Any]:
        """Read-only fingerprint of the page, used to tell whether a prompt really reached it."""
        page = await self.selector_test(profile_id)
        latest = page.get("latest_reply") or {}
        return {"mark": f"{latest.get('count')}:{latest.get('chars')}", "state": page.get("state")}

    async def create_pr(self, profile_id: str, *, repo: str, branch: str) -> dict[str, Any]:
        """Press Arena's Create PR for this chat after the page confirms it is the task's branch."""
        return await self.command(profile_id, "create_pr", repo=repo, branch=branch)

    async def close_feedback(self, profile_id: str) -> dict[str, Any]:
        return await self.command(profile_id, "close_feedback")

    async def read_last_reply(self, profile_id: str) -> dict[str, Any]:
        return await self.command(profile_id, "read_last_reply")

    async def selector_test(self, profile_id: str) -> dict[str, Any]:
        return await self.command(profile_id, "selector_test")

    async def run_until_quiet(self, profile_id: str, *, timeout: float, keep_working_used: int = 0, poll: float = 1.0) -> dict[str, Any]:
        """Monitor after a submit: continue through Keep working (within the configured limit) until idle."""
        deadline = time.monotonic() + timeout
        continued = 0
        while time.monotonic() < deadline:
            state = await self.state(profile_id)
            current = state.get("agent_state")
            if state.get("status") == "needs_human" or current == "needs_human":
                raise ArenaAgentError("needs_human", str(state.get("needs_human_reason") or "Arena page needs a human."))
            if current == "keep_working_visible":
                # Arena shows this option after every reply: a written reply is the finished answer.
                if str((await self.read_last_reply(profile_id)).get("text") or "").strip():
                    return {"state": current, "keep_working": continued, "limit_reached": False}
                if not keep_working_allowed(keep_working_used + continued):
                    return {"state": current, "keep_working": continued, "limit_reached": True}
                clicked = await self.keep_working(profile_id)
                continued += 1 if clicked.get("clicked") else 0
            elif current == "tool_call_limit":
                if not keep_working_allowed(keep_working_used + continued):
                    return {"state": current, "keep_working": continued, "limit_reached": True}
                await self.submit(profile_id, TOOL_LIMIT_CONTINUE)
                continued += 1
            elif current in {"idle", "pr_ready"}:
                return {"state": current, "keep_working": continued, "limit_reached": False}
            await asyncio.sleep(poll)
        raise ArenaAgentError("timeout", "Arena Agent did not finish in time.")

    # ------------------------------------------------------------------ provider API
    async def health_check(self) -> ProviderHealth:
        """R7: healthy only when the manager's Arena profile is connected and its Selector test passed.

        Otherwise the card is degraded and says why, in Arabic. Worker profiles are reported too.
        """
        from app.browser.arena_connector import NOT_ENABLED_MESSAGE, arena_gate, bridge_call
        from app.orchestration.manager_arena import manager_readiness

        t0 = time.perf_counter()
        gate = arena_gate()
        if not gate["enabled"]:
            ready = await manager_readiness()
            return ProviderHealth(self.provider_id, "degraded", 0.0, f"not_enabled: {NOT_ENABLED_MESSAGE} — {ready['reason_ar']}",
                                  credentials_configured=bool(self.resolve_credential()))
        res = await bridge_call("GET", "/v1/connector/profiles")
        latency = round((time.perf_counter() - t0) * 1000, 2)
        if not res.get("ok"):
            return ProviderHealth(self.provider_id, "offline", latency, f"{res.get('code')}: {res.get('message')}",
                                  credentials_configured=bool(self.resolve_credential()))
        agents = [p for p in res.get("profiles", []) if p.get("mode") == "agent"]
        connected = [p for p in agents if p.get("connected") and p.get("status") != "needs_human"]
        ready = await manager_readiness()
        return ProviderHealth(
            self.provider_id,
            "healthy" if ready["ready"] else "degraded",
            latency,
            f"{ready['reason_ar']} ({gate['mode']} mode, {len(connected)}/{len(agents)} Arena Agent profiles connected)",
            credentials_configured=bool(self.resolve_credential()),
        )

    def _fail(self, req: ProviderRequest, code: str, message: str, t0: float, **meta: Any) -> ProviderResponse:
        return ProviderResponse(
            provider_id=self.provider_id, model=req.model or self.default_model, content="", input_tokens=0,
            output_tokens=0, estimated_cost_usd=0.0, latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            success=False, error_message=message, metadata={"runtime": "arena_agent", "code": code, **meta}, retryable=False,
        )

    async def generate(self, req: ProviderRequest) -> ProviderResponse:
        """One prompt as a full Arena Agent cycle on the employee's own profile (agent mode only)."""
        from app.browser.arena_connector import NOT_ENABLED_MESSAGE, arena_gate

        t0 = time.perf_counter()
        if not arena_gate()["enabled"]:
            return self._fail(req, "not_enabled", NOT_ENABLED_MESSAGE, t0)
        meta = dict(req.metadata or {})
        profile_id = str(meta.get("browser_profile_id") or "").strip()
        if not profile_id:
            return self._fail(req, "profile_required", "Arena Agent needs the employee's browser profile.", t0)
        live = await self.state(profile_id)
        if live.get("mode") != "agent":
            return self._fail(req, "not_agent_mode", "This employee's profile is not in Arena Agent mode.", t0, browser_profile_id=profile_id)
        if req.dry_run:
            return self._fail(req, "dry_run", "Dry run: nothing was sent to Arena.", t0, dry_run=True)
        prompt = req.prompt if not req.system_prompt else f"{req.system_prompt}\n\n{req.prompt}"
        try:
            if meta.get("new_session"):
                await self.new_session(profile_id)
            await self.submit(profile_id, prompt)
            outcome = await self.run_until_quiet(profile_id, timeout=get_settings().ARENA_REPLY_TIMEOUT_SECONDS)
            reply = await self.read_last_reply(profile_id)
        except ArenaAgentError as exc:
            return self._fail(req, exc.code, f"Arena Agent: {exc.code}: {exc.message}", t0, browser_profile_id=profile_id,
                              connector_status="needs_human" if exc.code == "needs_human" else None)
        content = reply.get("text") or ""
        return ProviderResponse(
            provider_id=self.provider_id, model=req.model or self.default_model, content=content,
            input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(content) // 4), estimated_cost_usd=0.0,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2), success=bool(content),
            error_message=None if content else "Arena Agent returned no reply.",
            metadata={"runtime": "arena_agent", "browser_profile_id": profile_id, "keep_working": outcome.get("keep_working", 0),
                      "keep_working_limit_reached": outcome.get("limit_reached", False), "reply_truncated": reply.get("truncated", False)},
        )
