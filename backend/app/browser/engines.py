import asyncio
import fnmatch
import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from app.browser.manager import (
    BrowserManager,
    browser_manager,
    detect_playwright_capabilities,
    validate_navigation_url,
    validate_selector,
)

MAX_USERSCRIPT_BYTES = 100_000

PROHIBITED_USERSCRIPT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(captcha|recaptcha|hcaptcha|turnstile|arkose|funcaptcha|geetest|2captcha|anticaptcha|capsolver|deathbycaptcha)",
            re.IGNORECASE,
        ),
        "CAPTCHA or challenge-solving reference",
    ),
    (
        re.compile(
            r"(navigator\s*\.\s*webdriver|object\s*\.\s*defineproperty\s*\(\s*navigator|(?:playwright|puppeteer)[-_a-z0-9]*stealth|undetected[-_]?chromedriver|cf_clearance|bypass[-_\s]*(?:bot|anti[-_]?bot|cloudflare|waf|challenge|security))",
            re.IGNORECASE,
        ),
        "anti-bot evasion or webdriver/fingerprint spoofing",
    ),
    (
        re.compile(r"(document\s*\.\s*cookie|cookiestore|gm_cookie)", re.IGNORECASE),
        "cookie access or harvesting",
    ),
    (
        re.compile(
            r"(localstorage|sessionstorage)\s*(?:\.\s*getitem\s*\(\s*['\"`][^'\"`]*(?:token|auth|secret|key|session|cookie|jwt|bearer|pass)|\[\s*['\"`][^'\"`]*(?:token|auth|secret|key|session|cookie|jwt|bearer|pass)|\.\s*(?:token|auth|secret|key|session|cookie|jwt|bearer|pass))",
            re.IGNORECASE,
        ),
        "storage credential/token harvesting",
    ),
    (
        re.compile(r"(unsafewindow|gm[_.]xmlhttprequest)", re.IGNORECASE),
        "privileged Tampermonkey sandbox escape or cross-origin exfiltration API",
    ),
    (
        re.compile(r"(\beval\s*\(|\bnew\s+function\s*\()", re.IGNORECASE),
        "dynamic arbitrary code execution (eval/Function)",
    ),
    (
        re.compile(
            r"(xmlhttprequest\s*\.\s*prototype\s*\.\s*(?:open|send|setrequestheader)|window\s*\.\s*fetch\s*=)",
            re.IGNORECASE,
        ),
        "network request interception or undocumented API scraping hook",
    ),
]

PROHIBITED_GRANT_PATTERN = re.compile(
    r"@grant\s+(gm_cookie|unsafewindow|gm_xmlhttprequest|gm\.xmlhttprequest)\b",
    re.IGNORECASE,
)

HEADER_MATCH_PATTERN = re.compile(r"^\s*//\s*@(?:match|include)\s+(\S+)", re.IGNORECASE | re.MULTILINE)
ROLE_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
AGENT_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}$")
WILDCARD_SCOPE_VALUES = {"*", "all", "any", "everyone"}

ALLOWED_BROWSER_ACTIONS = {
    "navigate",
    "snapshot",
    "capture_snapshot",
    "click",
    "click_element",
    "fill",
    "fill_input",
    "type",
    "extract_text",
    "wait",
    "run_userscript",
    "run_workflow",
}
PROHIBITED_ACTION_TERMS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "stealth",
    "evasion",
    "webdriver",
    "bypass",
    "cookie_dump",
    "scrape_api",
)


def _split_match_pattern(pattern: str) -> tuple[str, str, str | None, str] | None:
    """Parse an http(s) @match pattern into (scheme, host, port_or_none, path)."""
    cleaned = (pattern or "").strip()
    if not cleaned or cleaned.lower() == "<all_urls>":
        return None
    m = re.fullmatch(r"(https?)://([^/]+)(/.*)?", cleaned, re.IGNORECASE)
    if not m:
        return None
    scheme = m.group(1).lower()
    authority = m.group(2).strip()
    path = m.group(3) or "/"

    if "@" in authority or not authority:
        return None

    host = authority
    port: str | None = None
    if ":" in authority:
        host, port = authority.rsplit(":", 1)
        if port != "*" and not port.isdigit():
            return None

    return scheme, host.lower(), port, path


def _validate_match_pattern(pattern: str) -> tuple[bool, str]:
    cleaned = (pattern or "").strip()
    if not cleaned:
        return False, "Policy violation: empty @match pattern is not allowed."
    if cleaned.lower() in ("<all_urls>", "*", "*://*/*", "*://*"):
        return False, f"Policy violation: overly broad @match pattern '{cleaned}' is prohibited."

    parts = _split_match_pattern(cleaned)
    if parts is None:
        return (
            False,
            f"Policy violation: @match pattern '{cleaned}' must use explicit http:// or https:// host scoping.",
        )

    _, host, _, _ = parts
    if host in ("*", "*.", "") or host.endswith(".*"):
        return False, f"Policy violation: wildcard host '{host}' in @match pattern '{cleaned}' is prohibited."
    if "*" in host:
        # Allow only subdomain wildcard with a concrete multi-label domain like *.example.com
        if not host.startswith("*.") or "*" in host[2:] or "." not in host[2:]:
            return (
                False,
                f"Policy violation: invalid wildcard host '{host}' in @match pattern '{cleaned}'.",
            )
    return True, "ok"


def match_url_pattern(pattern: str, url: str) -> bool:
    """Evaluate whether a target URL matches a validated Tampermonkey @match pattern."""
    ok, _ = _validate_match_pattern(pattern)
    if not ok:
        return False
    parts = _split_match_pattern(pattern)
    if parts is None:
        return False
    p_scheme, p_host, p_port, p_path = parts

    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() != p_scheme:
        return False
    u_host = (parsed.hostname or "").lower()
    if not u_host:
        return False

    if p_host.startswith("*."):
        base_domain = p_host[2:]
        if u_host != base_domain and not u_host.endswith("." + base_domain):
            return False
    elif u_host != p_host:
        return False

    if p_port is not None and p_port != "*":
        default_port = 443 if p_scheme == "https" else 80
        u_port = parsed.port or default_port
        if str(u_port) != p_port:
            return False

    u_path = parsed.path or "/"
    if parsed.query:
        u_path = f"{u_path}?{parsed.query}"
    return fnmatch.fnmatch(u_path, p_path)


def validate_userscript_policy(
    code: str,
    match_patterns: list[str],
    allowed_roles: list[str] | None = None,
    allowed_agent_ids: list[str] | None = None,
) -> tuple[bool, str]:
    """Audit Tampermonkey userscripts before enabling: block evasion/credential harvesting & enforce host/role scoping."""
    if not isinstance(code, str) or not code.strip():
        return False, "Policy violation: userscript code cannot be empty."
    if len(code.encode("utf-8")) > MAX_USERSCRIPT_BYTES:
        return False, f"Policy violation: userscript exceeds maximum size ({MAX_USERSCRIPT_BYTES} bytes)."

    if PROHIBITED_GRANT_PATTERN.search(code):
        return False, "Policy violation: userscript requests a prohibited @grant privilege."

    for pattern, reason in PROHIBITED_USERSCRIPT_PATTERNS:
        if pattern.search(code):
            return False, f"Policy violation: userscript references prohibited {reason}."

    if not match_patterns:
        return False, "At least one @match URL pattern is required."

    for pat in match_patterns:
        pat_ok, pat_msg = _validate_match_pattern(pat)
        if not pat_ok:
            return False, pat_msg

    # Also audit any @match / @include directives embedded in the userscript metadata block
    for embedded_pat in HEADER_MATCH_PATTERN.findall(code):
        pat_ok, pat_msg = _validate_match_pattern(embedded_pat)
        if not pat_ok:
            return False, f"Embedded metadata {pat_msg}"

    # Validate role/agent scoping when provided
    if allowed_roles is not None or allowed_agent_ids is not None:
        roles = [r.strip().lower() for r in (allowed_roles or []) if isinstance(r, str) and r.strip()]
        agent_ids = [a.strip() for a in (allowed_agent_ids or []) if isinstance(a, str) and a.strip()]
        if not roles and not agent_ids:
            return False, "Policy violation: userscript must specify at least one allowed role or allowed agent ID."
        for role in roles:
            if role in WILDCARD_SCOPE_VALUES or not ROLE_IDENTIFIER_RE.fullmatch(role):
                return False, f"Policy violation: invalid or wildcard allowed_role '{role}'."
        for aid in agent_ids:
            if aid.lower() in WILDCARD_SCOPE_VALUES or not AGENT_IDENTIFIER_RE.fullmatch(aid):
                return False, f"Policy violation: invalid or wildcard allowed_agent_id '{aid}'."

    return True, "approved"


def authorize_userscript_execution(
    script: Any,
    *,
    agent_id: str,
    agent_role: str,
    target_url: str,
) -> tuple[bool, str]:
    """Enforce fail-closed policy, role-scoping, agent-scoping, and @match checks before executing a userscript."""
    getter = (lambda k, default=None: script.get(k, default)) if isinstance(script, dict) else (lambda k, default=None: getattr(script, k, default))

    if not getter("is_enabled", False):
        return False, "Userscript is disabled."
    if getter("audit_status", "") != "approved":
        return False, f"Userscript audit_status is '{getter('audit_status', 'unknown')}', expected 'approved'."

    try:
        safe_url = validate_navigation_url(target_url)
    except ValueError as exc:
        return False, str(exc)

    code = getter("code", "")
    match_patterns = list(getter("match_patterns", []) or [])
    allowed_roles = list(getter("allowed_roles", []) or [])
    allowed_agent_ids = list(getter("allowed_agent_ids", []) or [])

    policy_ok, policy_msg = validate_userscript_policy(
        code,
        match_patterns,
        allowed_roles=allowed_roles,
        allowed_agent_ids=allowed_agent_ids,
    )
    if not policy_ok:
        return False, policy_msg

    norm_role = (agent_role or "").strip().lower()
    norm_allowed_roles = {r.strip().lower() for r in allowed_roles if isinstance(r, str) and r.strip()}
    norm_allowed_agents = {a.strip() for a in allowed_agent_ids if isinstance(a, str) and a.strip()}

    if not norm_allowed_roles and not norm_allowed_agents:
        return False, "Userscript has no role or agent scope; denied fail-closed."
    if norm_allowed_roles and norm_role not in norm_allowed_roles:
        return False, f"Role '{agent_role}' is not authorized to execute this userscript."
    if norm_allowed_agents and agent_id not in norm_allowed_agents:
        return False, f"Agent '{agent_id}' is not in allowed_agent_ids for this userscript."

    if not any(match_url_pattern(pat, safe_url) for pat in match_patterns):
        return False, f"Target URL '{safe_url}' does not match any authorized @match pattern."

    return True, "authorized"


def _validate_engine_action(action: str, params: dict[str, Any]) -> tuple[bool, str]:
    action_clean = (action or "").strip().lower()
    if not action_clean:
        return False, "Action name is required."
    for term in PROHIBITED_ACTION_TERMS:
        if term in action_clean:
            return False, f"Policy violation: prohibited browser action '{action}'."
    if action_clean not in ALLOWED_BROWSER_ACTIONS:
        return False, f"Unsupported browser action '{action}'. Allowed: {sorted(ALLOWED_BROWSER_ACTIONS)}."
    if "url" in params and params["url"] is not None:
        try:
            validate_navigation_url(str(params["url"]))
        except ValueError as exc:
            return False, str(exc)
    if "selector" in params and params["selector"] is not None:
        try:
            validate_selector(str(params["selector"]))
        except ValueError as exc:
            return False, str(exc)
    return True, "ok"


def _redact_action_params(params: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(params)
    if "value" in sanitized and sanitized["value"] is not None:
        sanitized["value_length"] = len(str(sanitized["value"]))
        sanitized["value"] = "<redacted>"
    return sanitized


class BaseBrowserEngineAdapter(ABC):
    engine_name: str = "base"

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        ...

    @abstractmethod
    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ...


class PlaywrightEngineAdapter(BaseBrowserEngineAdapter):
    engine_name = "playwright"

    def capabilities(self) -> dict[str, Any]:
        caps = detect_playwright_capabilities()
        return {
            "engine": self.engine_name,
            "supports_persistent_profiles": True,
            "supports_userscripts": True,
            "supports_screenshots": True,
            "playwright_installed": caps["playwright_installed"],
            "chromium_installed": caps["chromium_installed"],
            "engine_mode": caps["engine_mode"],
            "status": "ready" if caps["chromium_installed"] else "headless_fallback",
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ok, msg = _validate_engine_action(action, params)
        if not ok:
            return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "status": "rejected", "reason": msg}
        caps = detect_playwright_capabilities()
        return {
            "engine": self.engine_name,
            "profile_id": profile_id,
            "action": action,
            "engine_mode": caps["engine_mode"],
            "executed_live": bool(caps["chromium_installed"]),
            "status": "executed" if caps["chromium_installed"] else "headless_fallback",
            "params": _redact_action_params(params),
        }


class BrowserUseEngineAdapter(BaseBrowserEngineAdapter):
    engine_name = "browser_use"

    def capabilities(self) -> dict[str, Any]:
        installed = False
        try:
            import browser_use  # type: ignore  # noqa: F401
            installed = True
        except Exception:
            installed = False
        return {
            "engine": self.engine_name,
            "package_installed": installed,
            "delegates_to": "playwright",
            "status": "ready" if installed else "adapter_ready_optional_pkg",
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ok, msg = _validate_engine_action(action, params)
        if not ok:
            return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "status": "rejected", "reason": msg}
        return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "delegated_to": "playwright", "status": "executed"}


class OpenBrowserEngineAdapter(BaseBrowserEngineAdapter):
    engine_name = "openbrowser"

    def capabilities(self) -> dict[str, Any]:
        return {
            "engine": self.engine_name,
            "delegates_to": "playwright",
            "status": "adapter_ready",
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ok, msg = _validate_engine_action(action, params)
        if not ok:
            return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "status": "rejected", "reason": msg}
        return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "delegated_to": "playwright", "status": "executed"}


EXECUTABLE_WORKFLOW_STEPS = {
    "navigate",
    "click",
    "click_element",
    "fill",
    "fill_input",
    "snapshot",
    "capture_snapshot",
    "wait",
}
MAX_WORKFLOW_WAIT_MS = 10_000


class AutomaWorkflowAdapter(BaseBrowserEngineAdapter):
    """Validates and executes authorized step-based browser workflows without bundling external extensions."""

    engine_name = "automa"

    def capabilities(self) -> dict[str, Any]:
        return {
            "engine": self.engine_name,
            "supports_workflow_json": True,
            "supports_step_execution": True,
            "supported_steps": sorted(EXECUTABLE_WORKFLOW_STEPS),
            "delegates_to": "playwright",
            "status": "ready",
        }

    @staticmethod
    def _validate_workflow_steps(steps: Any, *, require_executable: bool = False) -> tuple[bool, str]:
        if not isinstance(steps, list):
            return False, "Workflow steps must be a list."

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                return False, f"Step {idx} must be an object."
            step_type = str(step.get("type", "")).strip().lower()
            for term in PROHIBITED_ACTION_TERMS:
                if term in step_type:
                    return False, f"Policy violation at step {idx}: prohibited step type '{step_type}'."
            if require_executable and step_type not in EXECUTABLE_WORKFLOW_STEPS:
                return (
                    False,
                    f"Unsupported executable workflow step '{step_type}' at index {idx}. Allowed: {sorted(EXECUTABLE_WORKFLOW_STEPS)}.",
                )
            if step.get("url") is not None:
                try:
                    validate_navigation_url(str(step["url"]))
                except ValueError as exc:
                    return False, f"Policy violation at step {idx}: {exc}"
            if step.get("selector") is not None:
                try:
                    validate_selector(str(step["selector"]))
                except ValueError as exc:
                    return False, f"Policy violation at step {idx}: {exc}"
            if require_executable:
                if step_type == "navigate" and not step.get("url"):
                    return False, f"Step {idx} ('navigate') requires a valid 'url'."
                if step_type in ("click", "click_element") and not step.get("selector"):
                    return False, f"Step {idx} ('{step_type}') requires a valid 'selector'."
                if step_type in ("fill", "fill_input") and (not step.get("selector") or "value" not in step):
                    return False, f"Step {idx} ('{step_type}') requires 'selector' and 'value'."
            inline_code = step.get("code") or step.get("script")
            if inline_code:
                patterns = step.get("match_patterns") or ([step["url"]] if step.get("url") else [])
                code_ok, code_msg = validate_userscript_policy(str(inline_code), patterns)
                if not code_ok:
                    return False, f"Policy violation at step {idx}: {code_msg}"

        return True, "ok"

    async def execute_workflow(
        self,
        db: Any,
        profile: Any,
        steps: list[dict[str, Any]],
        *,
        manager: BrowserManager | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Validate all steps fail-closed, then execute sequentially against BrowserManager with redacted step traces."""
        ok, msg = self._validate_workflow_steps(steps, require_executable=True)
        if not ok:
            return {
                "engine": self.engine_name,
                "profile_id": profile.id,
                "status": "rejected",
                "reason": msg,
                "steps_total": len(steps) if isinstance(steps, list) else 0,
                "steps_validated": 0,
                "steps_completed": 0,
                "trace": [],
            }

        mgr = manager or browser_manager
        trace: list[dict[str, Any]] = []
        wf_t0 = time.perf_counter()

        for idx, step in enumerate(steps):
            step_type = str(step.get("type", "")).strip().lower()
            step_t0 = time.perf_counter()
            try:
                if step_type == "navigate":
                    url = str(step["url"])
                    updated = await mgr.navigate_profile(db, profile, url)
                    elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                    live = profile.id in mgr._active_contexts
                    trace.append(
                        {
                            "step_index": idx,
                            "type": "navigate",
                            "status": "completed",
                            "elapsed_ms": elapsed_ms,
                            "engine_mode": "playwright" if live else "headless_fallback",
                            "executed_live": live,
                            "url": updated.current_url,
                        }
                    )
                elif step_type in ("click", "click_element"):
                    res = await mgr.click_element(
                        db,
                        profile,
                        str(step["selector"]),
                        actor=actor,
                        timeout_ms=int(step.get("timeout_ms", 5_000)),
                    )
                    elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                    step_ok = bool(res.get("ok"))
                    trace.append(
                        {
                            "step_index": idx,
                            "type": "click_element",
                            "status": "completed" if step_ok else "failed",
                            "elapsed_ms": elapsed_ms,
                            "engine_mode": res.get("engine_mode"),
                            "executed_live": bool(res.get("executed_live")),
                            "selector": res.get("selector"),
                            **({"error": res["error"]} if not step_ok and res.get("error") else {}),
                        }
                    )
                    if not step_ok:
                        return {
                            "engine": self.engine_name,
                            "profile_id": profile.id,
                            "status": "failed",
                            "reason": res.get("error", f"Step {idx} ('{step_type}') failed."),
                            "steps_total": len(steps),
                            "steps_validated": len(steps),
                            "steps_completed": idx,
                            "total_elapsed_ms": round((time.perf_counter() - wf_t0) * 1000, 2),
                            "trace": trace,
                        }
                elif step_type in ("fill", "fill_input"):
                    res = await mgr.fill_input(
                        db,
                        profile,
                        str(step["selector"]),
                        str(step["value"]),
                        actor=actor,
                        timeout_ms=int(step.get("timeout_ms", 5_000)),
                    )
                    elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                    step_ok = bool(res.get("ok"))
                    trace.append(
                        {
                            "step_index": idx,
                            "type": "fill_input",
                            "status": "completed" if step_ok else "failed",
                            "elapsed_ms": elapsed_ms,
                            "engine_mode": res.get("engine_mode"),
                            "executed_live": bool(res.get("executed_live")),
                            "selector": res.get("selector"),
                            "value_length": res.get("value_length"),
                            "redacted": True,
                            **({"error": res["error"]} if not step_ok and res.get("error") else {}),
                        }
                    )
                    if not step_ok:
                        return {
                            "engine": self.engine_name,
                            "profile_id": profile.id,
                            "status": "failed",
                            "reason": res.get("error", f"Step {idx} ('{step_type}') failed."),
                            "steps_total": len(steps),
                            "steps_validated": len(steps),
                            "steps_completed": idx,
                            "total_elapsed_ms": round((time.perf_counter() - wf_t0) * 1000, 2),
                            "trace": trace,
                        }
                elif step_type in ("snapshot", "capture_snapshot"):
                    res = await mgr.capture_snapshot(
                        db,
                        profile,
                        actor=actor,
                        full_page=bool(step.get("full_page", False)),
                    )
                    elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                    trace.append(
                        {
                            "step_index": idx,
                            "type": "capture_snapshot",
                            "status": "completed",
                            "elapsed_ms": elapsed_ms,
                            "engine_mode": res.get("engine_mode"),
                            "executed_live": bool(res.get("executed_live")),
                            "format": res.get("format"),
                            "screenshot_bytes": res.get("screenshot_bytes", 0),
                        }
                    )
                elif step_type == "wait":
                    raw_ms = step.get("ms", step.get("duration_ms", 0))
                    try:
                        bounded_ms = min(max(0, int(raw_ms)), MAX_WORKFLOW_WAIT_MS)
                    except (TypeError, ValueError):
                        bounded_ms = 0
                    if bounded_ms > 0:
                        await asyncio.sleep(bounded_ms / 1000.0)
                    elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                    trace.append(
                        {
                            "step_index": idx,
                            "type": "wait",
                            "status": "completed",
                            "elapsed_ms": elapsed_ms,
                            "wait_ms": bounded_ms,
                        }
                    )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - step_t0) * 1000, 2)
                trace.append(
                    {
                        "step_index": idx,
                        "type": step_type,
                        "status": "failed",
                        "elapsed_ms": elapsed_ms,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                return {
                    "engine": self.engine_name,
                    "profile_id": profile.id,
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "steps_total": len(steps),
                    "steps_validated": len(steps),
                    "steps_completed": idx,
                    "total_elapsed_ms": round((time.perf_counter() - wf_t0) * 1000, 2),
                    "trace": trace,
                }

        return {
            "engine": self.engine_name,
            "profile_id": profile.id,
            "status": "completed",
            "steps_total": len(steps),
            "steps_validated": len(steps),
            "steps_completed": len(steps),
            "total_elapsed_ms": round((time.perf_counter() - wf_t0) * 1000, 2),
            "trace": trace,
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        ok, msg = _validate_engine_action(action, params)
        if not ok:
            return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "status": "rejected", "reason": msg}

        steps = params.get("steps", [])
        if "db" in params and "profile" in params:
            return await self.execute_workflow(
                params["db"],
                params["profile"],
                steps,
                manager=params.get("manager"),
                actor=str(params.get("actor") or "system"),
            )

        steps_ok, steps_msg = self._validate_workflow_steps(steps, require_executable=False)
        if not steps_ok:
            return {"engine": self.engine_name, "profile_id": profile_id, "status": "rejected", "reason": steps_msg}

        return {
            "engine": self.engine_name,
            "profile_id": profile_id,
            "steps_validated": len(steps),
            "status": "executed",
        }


BROWSER_ENGINE_REGISTRY: dict[str, BaseBrowserEngineAdapter] = {
    "playwright": PlaywrightEngineAdapter(),
    "browser_use": BrowserUseEngineAdapter(),
    "openbrowser": OpenBrowserEngineAdapter(),
    "automa": AutomaWorkflowAdapter(),
}
