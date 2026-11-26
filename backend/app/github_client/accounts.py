"""GitHub accounts and the owner's repository registry.

* Accounts are the ones signed in to the GitHub CLI on this machine (`gh auth status`). Tokens stay in
  the CLI keyring: they are read only to run a `gh` command for a repository and are never returned,
  stored or logged.
* The registry is the list of repositories the owner added in the UI (system setting
  ``github_repositories``). Each entry names the account that works on it. Registered repositories join
  the GitHub allowlist, and every `gh` command that targets one runs with that account's token.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.async_subprocess import run_exec

SETTING_KEY = "github_repositories"
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_SLUG_IN_PATH_RE = re.compile(r"(?:^|/)repos/([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)")
_ACCOUNT_LINE_RE = re.compile(r"Logged in to (\S+) account (\S+)")
_TOKEN_TTL_SECONDS = 600

# repo slug (lower case) -> account login
_registry: dict[str, str] = {}
_token_cache: dict[str, tuple[float, str]] = {}


def valid_login(login: str | None) -> bool:
    return bool(login and _LOGIN_RE.fullmatch(login))


def set_registry(entries: Iterable[Mapping[str, Any]] | None) -> None:
    """Replace the in-memory registry from the stored setting value."""
    _registry.clear()
    for entry in entries or []:
        repo = str(entry.get("repo") or "").strip().lower()
        account = str(entry.get("account") or "").strip()
        if repo and "/" in repo:
            _registry[repo] = account


def registered_repos() -> set[str]:
    return set(_registry)


def account_for(repo: str | None) -> str | None:
    return _registry.get((repo or "").strip().lower()) or None


def repo_in_args(args: list[str]) -> str | None:
    """The `owner/name` a gh command targets: `--repo`/`-R` value or a `repos/owner/name/...` API path."""
    for i, arg in enumerate(args):
        if arg in ("--repo", "-R") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--repo="):
            return arg.split("=", 1)[1]
    for arg in args:
        m = _SLUG_IN_PATH_RE.search(arg)
        if m:
            return m.group(1)
    return None


async def token_for(account: str) -> str | None:
    if not valid_login(account):
        return None
    cached = _token_cache.get(account)
    if cached and time.monotonic() - cached[0] < _TOKEN_TTL_SECONDS:
        return cached[1]
    result = await run_exec(["gh", "auth", "token", "--user", account], timeout=15)
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        return None
    _token_cache[account] = (time.monotonic(), token)
    return token


async def env_for_account(account: str | None) -> dict[str, str] | None:
    """Environment that makes `gh` act as ``account``; None keeps the CLI's active account."""
    if not account:
        return None
    token = await token_for(account)
    if not token:
        return None
    return {**os.environ, "GH_TOKEN": token}


async def env_for_gh_args(args: list[str]) -> dict[str, str] | None:
    return await env_for_account(account_for(repo_in_args(args)))


async def list_accounts() -> list[dict[str, Any]]:
    """Accounts signed in to the GitHub CLI, without tokens."""
    result = await run_exec(["gh", "auth", "status"], timeout=15)
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    accounts: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _ACCOUNT_LINE_RE.search(line)
        if m:
            current = {"host": m.group(1), "login": m.group(2), "active": False, "scopes": []}
            accounts.append(current)
            continue
        if current is None:
            continue
        if line.startswith("- Active account:"):
            current["active"] = line.endswith("true")
        elif line.startswith("- Token scopes:"):
            current["scopes"] = re.findall(r"'([^']+)'", line)
    return [a for a in accounts if a["host"] == "github.com" and valid_login(a["login"])]
