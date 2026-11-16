"""Browser Wall SVG placeholder rendering."""

import html
from datetime import datetime, timezone

from app.models import BrowserProfile


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

