import asyncio
import base64
import html
import ipaddress
import json
import os
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from fastapi import HTTPException
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
MAX_EXTRACT_CHARS = 20_000
PROHIBITED_SELECTOR_RE = re.compile(
    r"(captcha|recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|cf-turnstile|g-recaptcha|h-captcha|px-captcha|datadome|challenge-form|javascript:|<script)",
    re.IGNORECASE,
)

SENSITIVE_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"\bAuthorization\s*:\s*[^\s,;\"']+(?:\s+[^\s,;\"']+)?", re.IGNORECASE), "Authorization: <redacted>"),
    (
        re.compile(
            r"\b((?:cookie|set-cookie|sessionid|session_id|csrftoken|cf_clearance|access_token|refresh_token|id_token|api_key|apikey|jwt|auth_token|password|passwd|secret)\s*[=:]\s*)([^\s;,&\"']+)",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted>"),
    (re.compile(r"\b(?:sk-(?:ant-|proj-)?|ghp_|gho_|github_pat_|xox[baprs]-|AIza|AKIA)[A-Za-z0-9_-]{12,}\b"), "<redacted>"),
]


def redact_extracted_text(text: str) -> tuple[str, int]:
    """Mask sensitive token, cookie, JWT, and credential patterns in extracted DOM text."""
    if not text:
        return "", 0
    redacted = text
    total_hits = 0
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        total_hits += count
    return redacted, total_hits


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


SSRF_DNS_TIMEOUT_SECONDS = 3.0
BLOCKED_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _require_public_ip(raw_address: str) -> None:
    try:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError("Navigation target resolved to an invalid IP address.") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global:
        raise ValueError("Navigation to loopback, private, link-local, or reserved IP addresses is blocked.")

async def validate_public_navigation_url(url: str) -> str:
    """Reject credentials and non-public destinations, including DNS names resolving to private IPs."""
    safe_url = validate_navigation_url(url)
    if safe_url == "about:blank":
        return safe_url
    parsed = urlparse(safe_url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("Navigation URLs must have a hostname and must not contain embedded credentials.")
    if host == "localhost" or host.endswith(BLOCKED_LOCAL_HOST_SUFFIXES):
        raise ValueError("Navigation to local or internal hostnames is blocked.")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValueError("Navigation URL contains an invalid port.") from exc

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        _require_public_ip(str(literal_address))
        return safe_url

    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
            timeout=SSRF_DNS_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError) as exc:
        raise ValueError("Navigation hostname could not be resolved safely.") from exc
    addresses = {str(record[4][0]) for record in records if record and len(record) > 4 and record[4]}
    if not addresses:
        raise ValueError("Navigation hostname did not resolve to an address.")
    for address in addresses:
        _require_public_ip(address)
    return safe_url

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
        "engine_mode": "playwright" if chromium_installed else "not_implemented",
        "browser_source": browser_source,
        "playwright_browsers_path": os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or None,
    }


def render_browser_wall_svg(profile: BrowserProfile, note: str = "") -> str:
    url = html.escape(profile.current_url or "about:blank")
    title = html.escape(profile.page_title or profile.name)
    name = html.escape(profile.name)
    status = html.escape(profile.status)
    mode = html.escape(profile.mode or "unavailable")
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
  <text x="46" y="196" fill="#e2e8f0" font-family="monospace" font-size="12">Engine: {html.escape(profile.engine_mode or "unavailable")} | Viewport: {profile.viewport_width}x{profile.viewport_height}</text>
  <text x="46" y="224" fill="#e2e8f0" font-family="monospace" font-size="12">Memory: not measured | Crashes: {profile.crash_count} | Heartbeat: {ts}</text>
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


    def has_live_context(self, profile_id: str) -> bool:
        return profile_id in self._active_contexts


    def _require_live_context(self, profile_id: str, action: str) -> None:
        if not self.has_live_context(profile_id):
            raise HTTPException(
                status_code=501,
                detail=f"Browser {action} is not implemented without a live Playwright context.",
            )


    async def _guard_browser_request(self, route: Any) -> None:
        """Validate every browser network request, including redirects and subresources."""
        request = route.request
        request_url = str(request.url)
        parsed = urlparse(request_url)
        is_navigation = getattr(request, "is_navigation_request", False)
        if callable(is_navigation):
            is_navigation = is_navigation()
        if parsed.scheme.lower() in {"data", "blob"} and not is_navigation:
            await route.continue_()
            return
        try:
            await validate_public_navigation_url(request_url)
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

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
                "service_workers": "block",
            }
            if caps.get("browser_source") == "system_binary" and caps.get("executable_path"):
                launch_kwargs["executable_path"] = caps["executable_path"]

            ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
            await ctx.route("**/*", self._guard_browser_request)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if url:
                safe_url = await validate_public_navigation_url(url)
                await page.goto(safe_url, timeout=10_000)
            actual_url = str(getattr(page, "url", "") or "about:blank")
            profile.current_url = await validate_public_navigation_url(actual_url)
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
            caps = detect_playwright_capabilities()
            if not caps["chromium_installed"]:
                raise HTTPException(
                    status_code=501,
                    detail="Browser launch is not implemented on this host because Playwright Chromium is unavailable.",
                )
            if safe_url is not None:
                safe_url = await validate_public_navigation_url(safe_url)

            # Enforce host concurrency limit when starting an inactive profile.
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

            # Prevent Chromium SingletonLock / process leaks when same owner re-launches.
            if profile.id in self._active_contexts or profile.status == "running":
                await self._close_active_context(profile.id)
                await self._close_db_sessions(db, profile.id, final_status="closed")

            if mode in ("visible", "background"):
                profile.mode = mode
            pdir = Path(profile.profile_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            launched_ok, err = await self._launch_playwright_context(profile, pdir, caps, safe_url)
            if not launched_ok:
                self._release_file_lock(profile)
                profile.status = "crashed"
                profile.lock_owner = None
                profile.engine_mode = "unavailable"
                profile.memory_mb = 0.0
                await self._close_db_sessions(db, profile.id, final_status="crashed")
                await db.commit()
                raise HTTPException(
                    status_code=503,
                    detail=f"Playwright failed to launch a live browser context ({type(err).__name__ if err else 'unknown error'}).",
                )

            profile.lock_owner = owner
            profile.engine_mode = "playwright"
            profile.status = "running"
            profile.memory_mb = 0.0  # No real process measurement is currently collected.
            profile.last_heartbeat = utcnow()
            launch_note = "Live Playwright Chromium context"
            if stale_lock_recovered:
                launch_note += " (recovered stale lock file)"
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
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before navigation (status='{profile.status}').")
            self._require_live_context(profile.id, "navigation")
            safe_url = await validate_public_navigation_url(safe_url)
            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(safe_url, timeout=10_000)
                page_title = await page.title()
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Playwright navigation failed ({type(exc).__name__}); profile state was not marked as navigated.",
                ) from None

            profile.current_url = safe_url
            profile.page_title = page_title or title or f"Page: {safe_url}"
            profile.last_heartbeat = utcnow()
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note="Live Playwright navigation completed.")
            db.add(
                AuditLog(
                    actor="system",
                    action="browser.navigate",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={"url": safe_url, "engine_mode": "playwright", "executed_live": True},
                )
            )
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
        """Click a selector in a live Playwright page, or return HTTP 501."""
        safe_selector = validate_selector(selector)
        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before clicking elements (status='{profile.status}').")
            self._require_live_context(profile.id, "click")

            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.click(safe_selector, timeout=timeout_ms)
                page_title = await page.title()
                live_url = str(getattr(page, "url", "") or "")
            except Exception as exc:
                error = type(exc).__name__
                db.add(
                    AuditLog(
                        actor=actor,
                        action="browser.click",
                        resource_type="browser_profile",
                        resource_id=profile.id,
                        details={
                            "selector": safe_selector,
                            "engine_mode": "playwright",
                            "executed_live": True,
                            "error": error,
                        },
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
                    "error": error,
                }

            if live_url:
                try:
                    profile.current_url = await validate_public_navigation_url(live_url)
                except ValueError:
                    # The request guard blocks private destinations; do not persist a blocked URL.
                    pass
            profile.page_title = page_title or profile.page_title
            profile.last_heartbeat = utcnow()
            note = f"Live Playwright click completed on '{safe_selector[:48]}'."
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.click",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "selector": safe_selector,
                        "engine_mode": "playwright",
                        "executed_live": True,
                    },
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
        """Fill a selector in a live Playwright page without logging the value, or return HTTP 501."""
        safe_selector = validate_selector(selector)
        if not isinstance(value, str):
            raise ValueError("Input value must be a string.")
        if len(value) > MAX_FILL_VALUE_LENGTH:
            raise ValueError(f"Input value exceeds maximum allowed length ({MAX_FILL_VALUE_LENGTH} chars).")
        timeout_ms = max(1, min(int(timeout_ms), 120_000))
        value_length = len(value)

        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before filling inputs (status='{profile.status}').")
            self._require_live_context(profile.id, "fill")

            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.fill(safe_selector, value, timeout=timeout_ms)
                page_title = await page.title()
            except Exception as exc:
                error = type(exc).__name__
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
                            "error": error,
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
                    "error": error,
                }

            profile.page_title = page_title or profile.page_title
            profile.last_heartbeat = utcnow()
            note = f"Live Playwright fill completed on '{safe_selector[:42]}' ({value_length} chars, redacted)."
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

    async def type_text(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        selector: str,
        value: str,
        *,
        actor: str = "system",
        timeout_ms: int = 5_000,
    ) -> dict[str, Any]:
        """Type text through a live Playwright page without logging the raw value."""
        safe_selector = validate_selector(selector)
        if not isinstance(value, str):
            raise ValueError("Input value must be a string.")
        if len(value) > MAX_FILL_VALUE_LENGTH:
            raise ValueError(f"Input value exceeds maximum allowed length ({MAX_FILL_VALUE_LENGTH} chars).")
        timeout_ms = max(1, min(int(timeout_ms), 120_000))

        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before typing (status='{profile.status}').")
            self._require_live_context(profile.id, "type")
            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.type(safe_selector, value, timeout=timeout_ms)
                page_title = await page.title()
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                db.add(
                    AuditLog(
                        actor=actor,
                        action="browser.type",
                        resource_type="browser_profile",
                        resource_id=profile.id,
                        details={"selector": safe_selector, "value_length": len(value), "redacted": True, "error": err_msg},
                        severity="warning",
                    )
                )
                await db.commit()
                return {
                    "ok": False,
                    "action": "type",
                    "profile_id": profile.id,
                    "selector": safe_selector,
                    "value_length": len(value),
                    "redacted": True,
                    "engine_mode": "playwright",
                    "executed_live": True,
                    "error": err_msg,
                }

            profile.page_title = page_title or profile.page_title
            profile.last_heartbeat = utcnow()
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note=f"Live Playwright type completed on '{safe_selector[:42]}' ({len(value)} chars, redacted).",
            )
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.type",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "selector": safe_selector,
                        "value_length": len(value),
                        "redacted": True,
                        "engine_mode": "playwright",
                        "executed_live": True,
                    },
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "type"})
            return {
                "ok": True,
                "action": "type",
                "profile_id": profile.id,
                "selector": safe_selector,
                "value_length": len(value),
                "redacted": True,
                "engine_mode": "playwright",
                "executed_live": True,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
            }

    async def capture_snapshot(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        *,
        actor: str = "system",
        full_page: bool = False,
    ) -> dict[str, Any]:
        """Capture an actual Playwright PNG; return 501 without a live browser context."""
        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(f"Browser profile '{profile.id}' must be running before snapshot capture (status='{profile.status}').")
            self._require_live_context(profile.id, "snapshot capture")
            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                png_bytes = await page.screenshot(full_page=full_page, type="png")
                page_title = await page.title()
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Playwright snapshot failed ({type(exc).__name__}); no synthetic snapshot was returned.",
                ) from None
            if not isinstance(png_bytes, bytes) or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(status_code=502, detail="Playwright did not return a valid PNG snapshot.")

            screenshot_b64 = base64.b64encode(png_bytes).decode("ascii")
            profile.page_title = page_title or profile.page_title
            profile.last_heartbeat = utcnow()
            note = f"Live Playwright PNG snapshot captured ({len(png_bytes)} bytes)."
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.snapshot",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "engine_mode": "playwright",
                        "executed_live": True,
                        "full_page": full_page,
                        "screenshot_bytes": len(png_bytes),
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
                "engine_mode": "playwright",
                "executed_live": True,
                "format": "png",
                "screenshot_base64": screenshot_b64,
                "screenshot_bytes": len(png_bytes),
                "svg": profile.last_snapshot_svg,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "viewport": {"width": profile.viewport_width, "height": profile.viewport_height},
                "lock_status": self.get_lock_status(profile),
            }

    async def extract_text(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        selector: str,
        max_chars: int = 2000,
        *,
        actor: str = "system",
        timeout_ms: int = 5_000,
    ) -> dict[str, Any]:
        """Extract and redact live DOM text; return 501 without an active Playwright context."""
        safe_selector = validate_selector(selector)
        bounded_max = max(1, min(int(max_chars), MAX_EXTRACT_CHARS))

        async with self._get_profile_lock(profile.id):
            if profile.status != "running":
                raise ValueError(
                    f"Browser profile '{profile.id}' must be running before extracting text (status='{profile.status}')."
                )
            self._require_live_context(profile.id, "text extraction")
            _, ctx = self._active_contexts[profile.id]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                if hasattr(page, "text_content"):
                    raw_text = await page.text_content(safe_selector, timeout=timeout_ms)
                elif hasattr(page, "inner_text"):
                    raw_text = await page.inner_text(safe_selector, timeout=timeout_ms)
                else:
                    raw_text = await page.locator(safe_selector).first.inner_text(timeout=timeout_ms)
                page_title = await page.title()
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Playwright text extraction failed ({type(exc).__name__}); no synthetic text was returned.",
                ) from None

            text_str = str(raw_text or "")
            masked_text, redacted_count = redact_extracted_text(text_str)
            truncated = len(masked_text) > bounded_max
            final_text = masked_text[:bounded_max]
            profile.page_title = page_title or profile.page_title
            profile.last_heartbeat = utcnow()
            note = (
                f"Live Playwright extract_text on '{safe_selector[:40]}' "
                f"({len(final_text)} chars, {redacted_count} redacted)."
            )
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note=note)
            db.add(
                AuditLog(
                    actor=actor,
                    action="browser.extract_text",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={
                        "selector": safe_selector,
                        "char_count": len(final_text),
                        "max_chars": bounded_max,
                        "truncated": truncated,
                        "redacted_count": redacted_count,
                        "engine_mode": "playwright",
                        "executed_live": True,
                    },
                )
            )
            await db.commit()
            await db.refresh(profile)
            await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "action": "extract_text"})
            return {
                "ok": True,
                "action": "extract_text",
                "profile_id": profile.id,
                "selector": safe_selector,
                "text": final_text,
                "char_count": len(final_text),
                "truncated": truncated,
                "redacted_count": redacted_count,
                "engine_mode": "playwright",
                "executed_live": True,
                "current_url": profile.current_url,
                "page_title": profile.page_title,
                "detail": note,
            }

    async def run_userscript(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        userscript: Any,
        agent_id: str,
        agent_role: str,
        *,
        target_url: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Userscripts are deliberately storage-only; browser-side code injection is disabled."""
        raise HTTPException(
            status_code=501,
            detail="Userscripts are storage only and are not injected or executed.",
        )

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
            profile.memory_mb = 0.0
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note="Browser crash detected; no live context is attached.",
            )
            await db.commit()

            if not recover:
                await db.refresh(profile)
                await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "status": profile.status})
                return profile

            caps = detect_playwright_capabilities()
            if not caps["chromium_installed"]:
                raise HTTPException(
                    status_code=501,
                    detail="Browser recovery is not implemented on this host because Playwright Chromium is unavailable.",
                )
            recovery_owner = profile.agent_id or "recovery-daemon"
            self._acquire_file_lock(profile, recovery_owner)
            pdir = Path(profile.profile_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            launched_ok, err = await self._launch_playwright_context(profile, pdir, caps, profile.current_url)
            if not launched_ok:
                self._release_file_lock(profile)
                profile.status = "crashed"
                profile.lock_owner = None
                profile.engine_mode = "unavailable"
                await db.commit()
                raise HTTPException(
                    status_code=503,
                    detail=f"Playwright recovery failed ({type(err).__name__ if err else 'unknown error'}).",
                )

            profile.status = "running"
            profile.lock_owner = recovery_owner
            profile.engine_mode = "playwright"
            profile.memory_mb = 0.0  # No real process measurement is currently collected.
            profile.last_heartbeat = utcnow()
            profile.last_snapshot_svg = render_browser_wall_svg(
                profile,
                note="Recovered with a live Playwright context; browser memory is not measured.",
            )
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
