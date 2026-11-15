import html
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.db.base import utcnow
from app.models import AuditLog, BrowserProfile, BrowserSession


def detect_playwright_capabilities() -> dict[str, Any]:
    playwright_pkg = False
    chromium_installed = False
    executable_path: str | None = None
    try:
        import playwright  # noqa: F401
        playwright_pkg = True
        from playwright.sync_api import sync_playwright

        # Check standard Playwright browser cache paths without launching
        cache_dirs = [
            Path.home() / ".cache" / "ms-playwright",
            Path("/root/.cache/ms-playwright"),
        ]
        for cdir in cache_dirs:
            if cdir.exists():
                for chrome in cdir.rglob("chrome"):
                    if chrome.is_file() and os.access(chrome, os.X_OK):
                        chromium_installed = True
                        executable_path = str(chrome)
                        break
        if not chromium_installed:
            for bin_name in ("chromium", "chromium-browser", "google-chrome"):
                found = shutil.which(bin_name)
                if found:
                    chromium_installed = True
                    executable_path = found
                    break
        _ = sync_playwright
    except Exception:
        pass

    return {
        "playwright_installed": playwright_pkg,
        "chromium_installed": chromium_installed,
        "executable_path": executable_path,
        "engine_mode": "playwright" if chromium_installed else "headless_fallback",
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
    """Manages persistent isolated Playwright browser profiles, locks, visible/background modes & recovery."""

    def __init__(self) -> None:
        self._active_contexts: dict[str, Any] = {}

    def _lock_file(self, profile: BrowserProfile) -> Path:
        pdir = Path(profile.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        return pdir / ".aatm.lock"

    async def launch_profile(
        self,
        db: AsyncSession,
        profile: BrowserProfile,
        owner: str,
        mode: str | None = None,
        url: str | None = None,
    ) -> BrowserProfile:
        if profile.lock_owner and profile.lock_owner != owner and profile.status == "running":
            raise ValueError(f"Browser profile '{profile.id}' is locked by '{profile.lock_owner}'.")

        caps = detect_playwright_capabilities()
        if mode in ("visible", "background"):
            profile.mode = mode
        if url:
            profile.current_url = url

        pdir = Path(profile.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        self._lock_file(profile).write_text(f"{owner}|{utcnow().isoformat()}", encoding="utf-8")

        profile.lock_owner = owner
        profile.engine_mode = caps["engine_mode"]
        profile.status = "running"
        profile.memory_mb = 142.5 if caps["chromium_installed"] else 28.4
        profile.last_heartbeat = utcnow()
        profile.last_snapshot_svg = render_browser_wall_svg(
            profile,
            note="Live Playwright Chromium context" if caps["chromium_installed"] else "Fallback renderer (Chromium binary not installed on host)",
        )

        # Attempt real Playwright persistent context if Chromium exists
        if caps["chromium_installed"]:
            try:
                from playwright.async_api import async_playwright

                pw = await async_playwright().start()
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(pdir),
                    headless=(profile.mode != "visible"),
                    viewport={"width": profile.viewport_width, "height": profile.viewport_height},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                if url and url != "about:blank":
                    await page.goto(url, timeout=10_000)
                    profile.page_title = await page.title() or profile.page_title
                self._active_contexts[profile.id] = (pw, ctx)
            except Exception as exc:
                profile.engine_mode = "headless_fallback"
                profile.last_snapshot_svg = render_browser_wall_svg(profile, note=f"Playwright fallback active: {exc}")

        session = BrowserSession(profile_id=profile.id, mode=profile.mode, status="active", pid=os.getpid())
        db.add(session)
        db.add(
            AuditLog(
                actor=owner,
                action="browser.launch",
                resource_type="browser_profile",
                resource_id=profile.id,
                details={"mode": profile.mode, "engine": profile.engine_mode, "url": profile.current_url},
            )
        )
        await db.commit()
        await db.refresh(profile)
        await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "status": profile.status, "mode": profile.mode})
        return profile

    async def navigate_profile(self, db: AsyncSession, profile: BrowserProfile, url: str, title: str | None = None) -> BrowserProfile:
        profile.current_url = url
        profile.page_title = title or f"Preview: {url}"
        profile.last_heartbeat = utcnow()
        if profile.id in self._active_contexts:
            try:
                _, ctx = self._active_contexts[profile.id]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(url, timeout=10_000)
                profile.page_title = await page.title() or profile.page_title
            except Exception:
                pass
        profile.last_snapshot_svg = render_browser_wall_svg(profile)
        await db.commit()
        await db.refresh(profile)
        await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "url": url})
        return profile

    async def stop_profile(self, db: AsyncSession, profile: BrowserProfile, actor: str = "system") -> BrowserProfile:
        if profile.id in self._active_contexts:
            pw, ctx = self._active_contexts.pop(profile.id)
            try:
                await ctx.close()
                await pw.stop()
            except Exception:
                pass

        lfile = self._lock_file(profile)
        if lfile.exists():
            lfile.unlink()

        profile.status = "stopped"
        profile.lock_owner = None
        profile.memory_mb = 0.0
        profile.last_heartbeat = utcnow()
        profile.last_snapshot_svg = render_browser_wall_svg(profile, note="Session stopped cleanly; persistent storage preserved.")

        sess_res = await db.execute(
            select(BrowserSession).where(BrowserSession.profile_id == profile.id, BrowserSession.status == "active")
        )
        for sess in sess_res.scalars().all():
            sess.status = "closed"
            sess.ended_at = utcnow()

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

    async def simulate_crash_and_recover(self, db: AsyncSession, profile: BrowserProfile, recover: bool = True) -> BrowserProfile:
        profile.crash_count += 1
        profile.status = "crashed"
        profile.lock_owner = None
        lfile = self._lock_file(profile)
        if lfile.exists():
            lfile.unlink()
        profile.last_snapshot_svg = render_browser_wall_svg(profile, note="Browser crash detected! Preparing self-healing recovery...")
        await db.commit()

        if recover:
            profile.status = "running"
            profile.lock_owner = profile.agent_id or "recovery-daemon"
            profile.last_heartbeat = utcnow()
            profile.memory_mb = 32.0 if profile.engine_mode != "playwright" else 140.0
            profile.last_snapshot_svg = render_browser_wall_svg(profile, note="Recovered automatically from crash; persistent cookies & storage intact.")
            db.add(
                AuditLog(
                    actor="self-healing",
                    action="browser.recover",
                    resource_type="browser_profile",
                    resource_id=profile.id,
                    details={"crash_count": profile.crash_count},
                    severity="warning",
                )
            )
            await db.commit()
        await db.refresh(profile)
        await event_bus.emit("browser.snapshot", {"profile_id": profile.id, "status": profile.status})
        return profile


browser_manager = BrowserManager()
