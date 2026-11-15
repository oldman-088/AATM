import asyncio
import base64
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import event_bus
from app.db.base import utcnow
from app.models import AuditLog, BrowserProfile, BrowserSession

SAFE_CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]

MAX_SELECTOR_LENGTH = 512
MAX_FILL_VALUE_LENGTH = 50_000
PROHIBITED_SELECTOR_RE = re.compile(
    r"(captcha|recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|cf-turnstile|g-recaptcha|h-captcha|px-captcha|datadome|challenge-form|javascript:|<script)",
    re.IGNORECASE,
)


def validate_selector(selector: str) -> str:
    """Validate DOM selectors for safe page interaction; block CAPTCHA/anti-bot challenge targets."""
    if not isinstance(selector, str):
        raise ValueError("Selector must be a non-empty string.")
    cleaned = selector.strip()
    if not cleaned:
        raise ValueError("Selector cannot be empty.")
    if len(cleaned) > MAX_SELECTOR_LENGTH:
        raise ValueError(f"Selector exceeds maximum allowed length ({MAX_SELECTOR_LENGTH} chars).")
    if PROHIBITED_SELECTOR_RE.search(cleaned):
        raise ValueError(
            "Policy violation: selector targets a prohibited CAPTCHA/anti-bot challenge or script pattern."
        )
    return cleaned


def validate_navigation_url(url: str) -> str:
    """Allow only safe web navigation schemes (http, https, about:blank)."""
    cleaned = (url or "").strip()
    if not cleaned or cleaned == "about:blank":
        return "about:blank"
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Blocked unsafe navigation URL scheme '{parsed.scheme or 'none'}': only http://, https://, and about:blank are allowed."
        )
    return cleaned


def _candidate_playwright_cache_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path and env_path != "0":
        dirs.append(Path(env_path))
    dirs.append(Path.home() / ".cache" / "ms-playwright")
    dirs.append(Path("/root/.cache/ms-playwright"))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        dirs.append(Path(local_app_data) / "ms-playwright")
    dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")

    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def detect_playwright_capabilities() -> dict[str, Any]:
    playwright_pkg = False
    chromium_installed = False
    executable_path: str | None = None
    browser_source: str | None = None
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright

        playwright_pkg = True

        # Search Playwright browser caches across Linux, Windows 11, and macOS without launching a browser
        preferred_names = ("chrome", "chrome.exe", "headless_shell", "headless_shell.exe")
        for cdir in _candidate_playwright_cache_dirs():
            if not cdir.exists():
                continue
            for exe_name in preferred_names:
                for candidate in cdir.rglob(exe_name):
                    if candidate.is_file() and (exe_name.endswith(".exe") or os.access(candidate, os.X_OK)):
                        chromium_installed = True
                        executable_path = str(candidate)
                        browser_source = "playwright_cache"
                        break
                if chromium_installed:
                    break
            if chromium_installed:
                break

        if not chromium_installed:
            for bin_name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
                found = shutil.which(bin_name)
                if found:
                    chromium_installed = True
                    executable_path = found
                    browser_source = "system_binary"
                    break
        _ = sync_playwright
    except Exception:
        pass

    return {
        "playwright_installed": playwright_pkg,
        "chromium_installed": chromium_installed,
        "executable_path": executable_path,
        "engine_mode": "playwright" if chromium_installed else "headless_fallback",
        "browser_source": browser_source,
        "playwright_browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or None,
    }


def render_browser_wall_svg(profile: BrowserProfile, note: str = "") -> str:
    url = html.escape(profile.current_url or "about:blank")
    title = html.escape(profile.page_title or profile.name)
    name = html.escape(profile.name)
    status = html.escape(profile.status)
    mode = html.escape(profile.mode)
    agent = html.escape(profile.agent_id or "unassigned")
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    status_color = {
        "running": "#22c55e",
        "ready": "#38bdf8",
        "locked": "#f59e0b",
        "crashed": "#ef4444",
        "stopped": "#64748b",
    }.get(profile.status, "#38bdf8")

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 380" width="640" height="380">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e293b"/>
    </linearGradient>
  </defs>
  <rect width="640" height="380" rx="12" fill="url(#bg)"/>
  <rect x="0" y="0" width="640" height="44" rx="12" fill="#0b1220"/>
  <circle cx="22" cy="22" r="6" fill="#ef4444"/>
  <circle cx="42" cy="22" r="6" fill="#f59e0b"/>
  <circle cx="62" cy="22" r="6" fill="#22c55e"/>
  <rect x="86" y="10" width="410" height="24" rx="6" fill="#1e293b" stroke="#334155"/>
  <text x="98" y="26" fill="#cbd5e1" font-family="monospace" font-size="11">{url[:56]}</text>
  <rect x="510" y="11" width="114" height="22" rx="11" fill="{status_color}" fill-opacity="0.2" stroke="{status_color}"/>
  <text x="567" y="26" text-anchor="middle" fill="{status_color}" font-family="sans-serif" font-size="11" font-weight="bold">{status.upper()}</text>
  <text x="28" y="84" fill="#f8fafc" font-family="sans-serif" font-size="20" font-weight="bold">{title[:42]}</text>
  <text x="28" y="110" fill="#94a3b8" font-family="sans-serif" font-size="13">Profile: {name} • Mode: {mode} • Agent: {agent}</text>
  <rect x="28" y="136" width="584" height="176" rx="8" fill="#0b1220" stroke="#334155"/>
  <text x="46" y="168" fill="#38bdf8" font-family="monospace" font-size="13">Persistent Dir: {html.escape(profile.profile_dir[-54:])}</text>
  <text x="46" y="196" fill="#e2e8f0" font-family="monospace" font-size="12">Engine: {html.escape(profile.engine_mode)} | Viewport: {profile.viewport_width}x{profile.viewport_height}</text>
  <text x="46" y="224" fill="#e2e8f0" font-family="monospace" font-size="12">Memory: {profile.memory_mb:.1f} MB | Crashes: {profile.crash_count} | Heartbeat: {ts}</text>
  <text x="46" y="262" fill="#94a3b8" font-family="sans-serif" font-size="12">{html.escape(note or 'Isolated Chromium persistent profile active and monitored by Browser Wall.')}</text>
  <text x="28" y="352" fill="#64748b" font-family="monospace" font-size="11">AATM Browser Wall • Profile ID: {html.escape(profile.id)}</text>
</svg>"""


class BrowserManager:
    """Manages persistent isolated Playwright browser profiles, DB+file+async locks, visible/background modes & recovery."""

    def __init__(self) -> None:
        self._active_contexts: dict[str, tuple[Any, Any]] = {}
        self._profile_locks: dict[str, asyncio.Lock] = {}

    def _get_profile_lock(self, profile_id: str) -> asyncio.Lock:
        lock = self._profile_locks.get(profile_id)
        if lock is None:
            lock = asyncio.Lock()
            self._profile_locks[profile_id] = lock
        return lock

    def _lock_file(self, profile: BrowserProfile) -> Path:
        pdir = Path(profile.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir / ".aatm.lock"

    @staticmethod
    def _is_pid_alive(pid: int | None) -> bool:
        if pid is None:
            return True
        if pid <= 0:
            return False
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            return False

    def _read_lock_file(self, profile: BrowserProfile) -> dict[str, Any] | None:
        lfile = Path(profile.profile_dir) / ".aatm.lock"
        if not lfile.exists():
            return None
        try:
            raw = lfile.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("owner"):
                return parsed
        except json.JSONDecodeError:
            pass
        # Backward compatibility with legacy "owner|timestamp" format
        parts = raw.split("|", 1)
        return {
            "owner": parts[0].strip(),
            "pid": None,
            "acquired_at": parts[1].strip() if len(parts) > 1 else None,
            "profile_id": profile.id,
        }

    def get_lock_status(self, profile: BrowserProfile) -> dict[str, Any]:
        file_lock = self._read_lock_file(profile)
        file_pid = file_lock.get("pid") if file_lock else None
        pid_alive = self._is_pid_alive(file_pid) if file_lock else False
        return {
            "profile_id": profile.id,
            "db_status": profile.status,
            "db_lock_owner": profile.lock_owner,
            "file_lock_present": file_lock is not None,
            "file_lock_owner": file_lock.get("owner") if file_lock else None,
            "file_lock_pid": file_pid,
            "file_lock_alive": pid_alive,
            "active_playwright_context": profile.id in self._active_contexts,
        }

    def _acquire_file_lock(self, profile: BrowserProfile, owner: str) -> bool:
        """Acquire profile directory lock atomically; returns True if a stale lock was recovered."""
        lfile = self._lock_file(profile)
        stale_recovered = False
        existing = self._read_lock_file(profile)
        if existing:
            existing_owner = existing.get("owner")
            existing_pid = existing.get("pid")
            if existing_owner and existing_owner != owner:
                if self._is_pid_alive(existing_pid):
                    raise ValueError(
                        f"Browser profile '{profile.id}' is file-locked by '{existing_owner}'"
                        + (f" (pid={existing_pid})." if existing_pid is not None else ".")
                    )
                stale_recovered = True
            try:
                lfile.unlink(missing_ok=True)
            except OSError:
                pass

        payload = json.dumps(
            {
                "owner": owner,
                "pid": os.getpid(),
                "acquired_at": utcnow().isoformat(),
                "profile_id": profile.id,
            }
        )
        try:
            fd = os.open(str(lfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            racer = self._read_lock_file(profile)
            racer_owner = racer.get("owner") if racer else "another-worker"
            raise ValueError(f"Browser profile '{profile.id}' is concurrently locked by '{racer_owner}'.")
        return stale_recovered

    def _release_file_lock(self, profile: BrowserProfile) -> None:
        lfile = Path(profile.profile_dir) / ".aatm.lock"
        try:
            lfile.unlink(missing_ok=True)
        except OSError:
            pass

    async def _close_active_context(self, profile_id: str) -> None:
        pair = self._active_contexts.pop(profile_id, None)
        if not pair:
            return
        pw, ctx = pair
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass

    async def _launch_playwright_context(
        self,
        profile: BrowserProfile,
        pdir: Path,
        caps: dict[str, Any],
        url: str | None,
    ) -> tuple[bool, str | None]:
        pw = None
        ctx = None
        try:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(pdir),
                "headless": (profile.mode != "visible"),
                "viewport": {"width": profile.viewport_width, "height": profile.viewport_height},
                "args": list(SAFE_CHROMIUM_ARGS),
            }
            if caps.get("browser_source") == "system_binary" and caps.get("executable_path"):
                launch_kwargs["executable_path"] = caps["executable_path"]

            ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if url and url != "about:blank":
                await page.goto(url, timeout=10_000)
                profile.page_title = await page.title() or profile.page_title
            self._active_contexts[profile.id] = (pw, ctx)
            return True, None
        except Exception as exc:
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
            return False, str(exc)

    async def _close_db_sessions(self, db: AsyncSession, profile_id: str, final_status: str = "closed") -> None:
        sess_res = await db.execute(
            select(BrowserSession).where(BrowserSession.profile_id == profile_id, BrowserSession.status == "active")
        )
        for sess in sess_res.scalars().all():
            sess.status = final_status
            sess.ended_at = utcnow()

    async def launch_profile(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        owner: str,
        mode: str | None = None,
        url: str | None = None,
    ) -> BrowserProfile:
        async with self._get_profile_lock(profile.id):
            if profile.lock_owner and profile.lock_owner != owner and profile.status == "running":
                raise ValueError(f"Browser profile '{profile.id}' is locked by '{profile.lock_owner}'.")

            safe_url = validate_navigation_url(url) if url is not None else None

            # Enforce host concurrency limit when starting an inactive profile
            if profile.status != "running":
                settings = get_settings()
                running_res = await db.execute(
                    select(func.count(BrowserProfile.id)).where(BrowserProfile.status == "running")
                )
                running_count = int(running_res.scalar_one() or 0)
                if running_count >= settings.MAX_CONCURRENT_BROWSERS:
                    raise ValueError(
                        f"Max concurrent browser limit ({settings.MAX_CONCURRENT_BROWSERS}) reached; stop an active profile first."
                    )

            stale_lock_recovered = self._acquire_file_lock(profile, owner)

            # Prevent Chromium SingletonLock / process leaks when same owner re-launches
            if profile.id in self._active_contexts or profile.status == "running":
                await self._close_active_context(profile.id)
                await self._close_db_sessions(db, profile.id, final_status="closed")

            caps = detect_playwright_capabilities()
            if mode in ("visible", "background"):
                profile.mode = mode
            if safe_url:
                profile.current_url = safe_url

            pdir = Path(profile.profile_dir)
            pdir.mkdir(parents=True, exist_ok=True)

            profile.lock_owner = owner
            profile.engine_mode = caps["engine_mode"]
            profile.status = "running"
            profile.memory_mb = 142.5 if caps["chromium_installed"] else 28.4
            profile.last_heartbeat = utcnow()
            launch_note = (
                "Live Playwright Chromium context"
                if caps["chromium_installed"]
                else "Fallback renderer (Chromium binary not installed on host)"
            )
            if stale_lock_recovered:
                launch_note += " (recovered stale lock file)"

            # Attempt real Playwright persistent context if Chromium exists
            if caps["chromium_installed"]:
                launched_ok, err = await self._launch_playwright_context(profile, pdir, caps, safe_url)
                if not launched_ok:
                    profile.engine_mode = "headless_fallback"
                    profile.memory_mb = 28.4
                    launch_note = f"Playwright fallback active: {err}"

            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=launch_note)

            session = BrowserSession(profile_id=profile.id, mode=profile.mode, status="active", pid=os.getpid())
            db.add(session)
            db.add(
                AuditLog(
                    actor=owner,
                    action="browser.launch",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "mode": profile.mode,
                        "engine": profile.engine_mode,
                        "url": profile.current_url,
                        "stale_lock_recovered": stale_lock_recovered,
                    },
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit(
                "browser.snapshot",
                {"profile_id": profile.id, "status": profile.status, "mode": profile.mode},
            )
            return profile

    async def navigate_profile(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        url: str,
        title: str | None = None,
    ) -> BrowserProfile:
        safe_url = validate_navigation_url(url)
        async with self._get_profile_lock(profile.id):
            profile.current_url = safe_url
            profile.page_title = title or f"Preview: {safe_url}"
            profile.last_heartbeat = utcnow()
            nav_note = ""
            if profile.id in self._active_contexts:
                try:
                    _, ctx = self._active_contexts[profile.id]
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    if safe_url != "about:blank":
                        await page.goto(safe_url, timeout=10_000)
                        profile.page_title = await page.title() or profile.page_title
                except Exception as exc:
                    nav_note = f"Navigation warning: {type(exc).__name__}"
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=nav_note)
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "url": safe_url})
            return profile

    async def click_element(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        selector: str,
        *,
        actor: str = "system",
        timeout_ms: int = 5_000,
    ) -> dict[str, Any]:
        """Policy-checked element click: runs via Playwright when live, or returns structured headless_fallback diagnostics."""
        safe_selector = validate_selector(selector)
        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before clicking elements (status='{profile.status}').")

            profile.last_heartbeat = utcnow()
            if profile.id in self._active_contexts:
                _, ctx = self._active_contexts[profile.id]
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.click(safe_selector, timeout=timeout_ms)
                    live_url = getattr(page, "url", None)
                    if isinstance(live_url, str) and live_url:
                        try:
                            profile.current_url = validate_navigation_url(live_url)
                        except ValueError:
                            pass
                    title = await page.title()
                    if title:
                        profile.page_title = title
                    note = f"Live Playwright click succeeded on '{safe_selector[:48]}'."
                    profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
                    db.add(
                        AuditLog(
                            actor=actor,
                            action="browser.click",
                            resource_type="browser_profile",
                            resource_id=profile.id,
                            details={"selector": safe_selector, "engine_mode": "playwright", "executed_live": True},
                        )
                    )
                    await db.commit()
                    await db.refresh(profile)
                    await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "click_element"})
                    return {
                        "ok": True,
                        "action": "click_element",
                        "profile_id": profile.id,
                        "selector": safe_selector,
                        "engine_mode": "playwright",
                        "executed_live": True,
                        "current_url": profile.current_url,
                        "page_title": profile.page_title,
                        "detail": note,
                    }
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                    profile.last_snapshot_svg = render_browser_wall_svg(
                        profile, note=f"Playwright click error: {type(exc).__name__}"
                    )
                    db.add(
                        AuditLog(
                            actor=actor,
                            action="browser.click",
                            resource_type="browser_profile",
                            resource_id=profile.id,
                            details={"selector": safe_selector, "engine_mode": "playwright", "error": err_msg},
                            severity="warning",
                        )
                    )
                    await db.commit()
                    return {
                        "ok": False,
                        "action": "click_element",
                        "profile_id": profile.id,
                        "selector": safe_selector,
                        "engine_mode": "playwright",
                        "executed_live": True,
                        "current_url": profile.current_url,
                        "page_title": profile.page_title,
                        "error": err_msg,
                    }

            caps = detect_playwright_capabilities()
            fallback_reason = (
                "Chromium binary not installed on host; operating in headless_fallback diagnostic mode."
                if not caps["chromium_installed"]
                else "No live Playwright context attached to this profile; operating in headless_fallback diagnostic mode."
            )
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note=f"[headless_fallback] Click diagnostic validated for '{safe_selector[:44]}'.",
            )
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.click",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={"selector": safe_selector, "engine_mode": "headless_fallback", "executed_live": False},
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "click_element"})
            return {
                "ok": True,
                "action": "click_element",
                "profile_id": profile.id,
                "selector": safe_selector,
                "engine_mode": "headless_fallback",
                "executed_live": False,
                "fallback_reason": fallback_reason,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "capabilities": caps,
            }

    async def fill_input(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        selector: str,
        value: str,
        *,
        actor: str = "system",
        timeout_ms: int = 5_000,
    ) -> dict[str, Any]:
        """Policy-checked input fill: runs via Playwright when live, never logs raw input text, or returns headless_fallback diagnostics."""
        safe_selector = validate_selector(selector)
        if not isinstance(value, str):
            raise ValueError("Input value must be a string.")
        if len(value) > MAX_FILL_VALUE_LENGTH:
            raise ValueError(f"Input value exceeds maximum allowed length ({MAX_FILL_VALUE_LENGTH} chars).")

        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before filling inputs (status='{profile.status}').")

            profile.last_heartbeat = utcnow()
            value_length = len(value)

            if profile.id in self._active_contexts:
                _, ctx = self._active_contexts[profile.id]
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await page.fill(safe_selector, value, timeout=timeout_ms)
                    title = await page.title()
                    if title:
                        profile.page_title = title
                    note = f"Live Playwright fill succeeded on '{safe_selector[:42]}' ({value_length} chars, redacted)."
                    profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
                    db.add(
                        AuditLog(
                            actor=actor,
                            action="browser.fill",
                            resource_type="browser_profile",
                            resource_id=profile.id,
                            details={
                                "selector": safe_selector,
                                "value_length": value_length,
                                "redacted": True,
                                "engine_mode": "playwright",
                                "executed_live": True,
                            },
                        )
                    )
                    await db.commit()
                    await db.refresh(profile)
                    await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "fill_input"})
                    return {
                        "ok": True,
                        "action": "fill_input",
                        "profile_id": profile.id,
                        "selector": safe_selector,
                        "value_length": value_length,
                        "redacted": True,
                        "engine_mode": "playwright",
                        "executed_live": True,
                        "current_url": profile.current_url,
                        "page_title": profile.page_title,
                        "detail": note,
                    }
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                    profile.last_snapshot_svg = render_browser_wall_svg(
                        profile, note=f"Playwright fill error: {type(exc).__name__}"
                    )
                    db.add(
                        AuditLog(
                            actor=actor,
                            action="browser.fill",
                            resource_type="browser_profile",
                            resource_id=profile.id,
                            details={
                                "selector": safe_selector,
                                "value_length": value_length,
                                "redacted": True,
                                "engine_mode": "playwright",
                                "error": err_msg,
                            },
                            severity="warning",
                        )
                    )
                    await db.commit()
                    return {
                        "ok": False,
                        "action": "fill_input",
                        "profile_id": profile.id,
                        "selector": safe_selector,
                        "value_length": value_length,
                        "redacted": True,
                        "engine_mode": "playwright",
                        "executed_live": True,
                        "current_url": profile.current_url,
                        "page_title": profile.page_title,
                        "error": err_msg,
                    }

            caps = detect_playwright_capabilities()
            fallback_reason = (
                "Chromium binary not installed on host; operating in headless_fallback diagnostic mode."
                if not caps["chromium_installed"]
                else "No live Playwright context attached to this profile; operating in headless_fallback diagnostic mode."
            )
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note=f"[headless_fallback] Fill diagnostic validated for '{safe_selector[:40]}' ({value_length} chars).",
            )
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.fill",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "selector": safe_selector,
                        "value_length": value_length,
                        "redacted": True,
                        "engine_mode": "headless_fallback",
                        "executed_live": False,
                    },
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "fill_input"})
            return {
                "ok": True,
                "action": "fill_input",
                "profile_id": profile.id,
                "selector": safe_selector,
                "value_length": value_length,
                "redacted": True,
                "engine_mode": "headless_fallback",
                "executed_live": False,
                "fallback_reason": fallback_reason,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "capabilities": caps,
            }

    async def capture_snapshot(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        *,
        actor: str = "system",
        full_page: bool = False,
    ) -> dict[str, Any]:
        """Capture live Playwright PNG + SVG snapshot when available, or deterministic SVG headless_fallback diagnostics."""
        async with self._get_profile_lock(profile.id):
            profile.last_heartbeat = utcnow()
            screenshot_b64: str | None = None
            screenshot_bytes = 0
            executed_live = False
            fallback_reason: str | None = None
            engine_mode = "headless_fallback"

            if profile.id in self._active_contexts:
                _, ctx = self._active_contexts[profile.id]
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    png_bytes = await page.screenshot(full_page=full_page, type="png")
                    screenshot_bytes = len(png_bytes)
                    screenshot_b64 = base64.b64encode(png_bytes).decode("ascii")
                    title = await page.title()
                    if title:
                        profile.page_title = title
                    executed_live = True
                    engine_mode = "playwright"
                except Exception as exc:
                    fallback_reason = f"Playwright screenshot fallback: {type(exc).__name__}"

            caps = detect_playwright_capabilities()
            if not executed_live and fallback_reason is None:
                fallback_reason = (
                    "Chromium binary not installed on host; returning deterministic SVG Browser Wall snapshot."
                    if not caps["chromium_installed"]
                    else "No live Playwright context attached to this profile; returning deterministic SVG snapshot."
                )

            note = (
                f"Live Playwright PNG snapshot captured ({screenshot_bytes} bytes)."
                if executed_live
                else f"[headless_fallback] {fallback_reason}"
            )
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.snapshot",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "engine_mode": engine_mode,
                        "executed_live": executed_live,
                        "full_page": full_page,
                        "screenshot_bytes": screenshot_bytes,
                    },
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "capture_snapshot"})

            return {
                "ok": True,
                "action": "capture_snapshot",
                "profile_id": profile.id,
                "engine_mode": engine_mode,
                "executed_live": executed_live,
                "format": "png+svg" if executed_live else "svg",
                "screenshot_base64": screenshot_b64,
                "screenshot_bytes": screenshot_bytes,
                "svg": profile.last_snapshot_svg,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "viewport": {"width": profile.viewport_width, "height": profile.viewport_height},
                "lock_status": self.get_lock_status(profile),
                "capabilities": caps,
                "fallback_reason": fallback_reason,
            }

    async def stop_profile(self, db: AsyncSession, profile: BrowserProfile, actor: str = "system") -> BrowserProfile:
        async with self._get_profile_lock(profile.id):
            await self._close_active_context(profile.id)
            self._release_file_lock(profile)

            profile.status = "stopped"
            profile.lock_owner = None
            profile.memory_mb = 0.0
            profile.last_heartbeat = utcnow()
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note="Session stopped cleanly; persistent storage preserved.",
            )

            await self._close_db_sessions(db, profile.id, final_status="closed")

            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.stop",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={"status": "stopped"},
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "status": "stopped"})
            return profile

    async def simulate_crash_and_recover(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        recover: bool = True,
    ) -> BrowserProfile:
        async with self._get_profile_lock(profile.id):
            await self._close_active_context(profile.id)
            await self._close_db_sessions(db, profile.id, final_status="crashed")
            self._release_file_lock(profile)

            profile.crash_count += 1
            profile.status = "crashed"
            profile.lock_owner = None
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note="Browser crash detected! Preparing self-healing recovery...",
            )
            await db.commit()

            if recover:
                recovery_owner = profile.agent_id or "recovery-daemon"
                self._acquire_file_lock(profile, recovery_owner)
                caps = detect_playwright_capabilities()
                pdir = Path(profile.profile_dir)
                pdir.mkdir(parents=True, exist_ok=True)

                profile.status = "running"
                profile.lock_owner = recovery_owner
                profile.engine_mode = caps["engine_mode"]
                profile.last_heartbeat = utcnow()
                profile.memory_mb = 140.0 if caps["chromium_installed"] else 32.0
                recovery_note = "Recovered automatically from crash; persistent cookies & storage intact."

                if caps["chromium_installed"]:
                    launched_ok, err = await self._launch_playwright_context(
                        profile, pdir, caps, profile.current_url
                    )
                    if not launched_ok:
                        profile.engine_mode = "headless_fallback"
                        profile.memory_mb = 32.0
                        recovery_note = f"Recovered in fallback mode: {err}"

                profile.last_snapshot_svg = render_browser_wall_svg(profile, note=recovery_note)
                db.add(
                    BrowserSession(
                        profile_id=profile.id,
                        mode=profile.mode,
                        status="active",
                        pid=os.getpid(),
                        recovery_notes=f"Auto-recovered after crash #{profile.crash_count}",
                    )
                )
                db.add(
                    AuditLog(
                        actor="self-healing",
                        action="browser.recover",
                        resource_type="browser_profile",
                        resource_id=profile.id,
                        details={"crash_count": profile.crash_count, "engine": profile.engine_mode},
                        severity="warning",
                    )
                )
                await db.commit()

            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "status": profile.status})
            return profile

    async def shutdown_all(self) -> int:
        """Gracefully close all active Playwright contexts held by this process."""
        profile_ids = list(self._active_contexts.keys())
        for pid in profile_ids:
            await self._close_active_context(pid)
        return len(profile_ids)


browser_manager = BrowserManager()
