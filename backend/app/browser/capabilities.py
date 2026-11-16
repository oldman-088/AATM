"""Playwright / Chromium capability detection."""

import os
import shutil
from pathlib import Path
from typing import Any


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

