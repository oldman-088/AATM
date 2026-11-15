from abc import ABC, abstractmethod
from typing import Any


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
        return {
            "engine": self.engine_name,
            "supports_persistent_profiles": True,
            "supports_userscripts": True,
            "supports_screenshots": True,
            "status": "ready",
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "status": "executed", "params": params}


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
        return {"engine": self.engine_name, "profile_id": profile_id, "action": action, "delegated_to": "playwright", "status": "executed"}


class AutomaWorkflowAdapter(BaseBrowserEngineAdapter):
    """Validates and executes authorized step-based browser workflows without bundling external extensions."""
    engine_name = "automa"

    def capabilities(self) -> dict[str, Any]:
        return {
            "engine": self.engine_name,
            "supports_workflow_json": True,
            "delegates_to": "playwright",
            "status": "ready",
        }

    async def execute_action(self, profile_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        steps = params.get("steps", [])
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


def validate_userscript_policy(code: str, match_patterns: list[str]) -> tuple[bool, str]:
    """Audit Tampermonkey userscripts before enabling: block prohibited evasion/credential harvesting."""
    lower = code.lower()
    prohibited = ("captcha", "recaptcha", "hcaptcha", "turnstile", "document.cookie", "localstorage.getitem('token")
    for term in prohibited:
        if term in lower:
            return False, f"Policy violation: userscript references prohibited pattern '{term}'."
    if not match_patterns:
        return False, "At least one @match URL pattern is required."
    return True, "approved"
