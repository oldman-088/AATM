"""manager-0 on Arena Agent mode (R7): the manager's own Arena session, driven like the workers'."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.browser.arena_connector import MANAGER_PROFILE_ID, NOT_ENABLED_MESSAGE, arena_gate, bridge_call
from app.core.config import get_settings
from app.models import SystemSetting

MANAGER_PROVIDERS = ("arena_agent", "anthropic_claude", "openai_compatible", "demo")
MANAGER_PROVIDER_SETTING = "manager_provider"

NOT_CONNECTED_AR = "المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير"
READINESS_AR = {
    "not_enabled": "موصّل Arena متوقف (ARENA_CONNECTOR_ENABLED=false) — لا يمكن تشغيل المدير عبر Arena.",
    "bridge_unreachable": "الجسر المحلي لـ Arena غير متاح — شغّل الجسر ثم أعد الفحص.",
    "not_registered": "لم يُولَّد سكربت ملف المدير بعد — افتح بطاقة المدير في تبويب المتصفحات واضغط «توليد السكربت».",
    "not_agent_mode": "ملف المدير ليس في وضع وكيل Arena.",
    "offline": NOT_CONNECTED_AR,
    "needs_human": "ملف المدير يحتاج تدخلًا بشريًا: ",
    "selector_not_run": "المدير متصل، لكن لم يُشغَّل «اختبار المحددات» على ملفه بعد — شغّله من بطاقة المدير.",
    "selector_failed": "فشل «اختبار المحددات» على ملف المدير — اطلب من المالك فحص صفحة Arena قبل المتابعة.",
    "ready": "المدير متصل بـ Arena واختبار المحددات ناجح.",
}


async def manager_provider_mode(db: AsyncSession) -> str:
    """Effective manager runtime: AATM_MANAGER_PROVIDER, else the wizard's choice, else '' (legacy)."""
    configured = (get_settings().AATM_MANAGER_PROVIDER or "").strip()
    if configured:
        return configured
    row = await db.get(SystemSetting, MANAGER_PROVIDER_SETTING)
    value = str((row.value or {}).get("provider_type") or "") if row is not None else ""
    return value if value in MANAGER_PROVIDERS else ""


async def manager_readiness(profile_id: str = MANAGER_PROFILE_ID) -> dict[str, Any]:
    """Is the manager's Arena profile usable? Connected, not blocked, agent mode, Selector test passed.

    Read-only: it asks the bridge for the profile's live state and stored Selector test report; it
    never sends anything to the Arena page.
    """
    base: dict[str, Any] = {"profile_id": profile_id, "connected": False, "selector_ok": False, "selector_ran": False}

    def verdict(code: str, **extra: Any) -> dict[str, Any]:
        reason = READINESS_AR[code] + (str(extra.pop("detail", "")) if code == "needs_human" else "")
        return {**base, **extra, "ready": code == "ready", "code": code, "reason_ar": reason}

    if not arena_gate()["enabled"]:
        return verdict("not_enabled", detail_en=NOT_ENABLED_MESSAGE)
    res = await bridge_call("GET", f"/v1/connector/profiles/{profile_id}")
    if not res.get("ok"):
        code = str(res.get("code") or "")
        if code == "not_registered":
            return verdict("not_registered")
        if code == "bridge_unreachable" or code.startswith("http_"):
            return verdict("bridge_unreachable", detail_en=str(res.get("message") or code))
        return verdict("offline", detail_en=str(res.get("message") or code))
    selector = res.get("selector_report") or None
    state = {"connected": bool(res.get("connected")), "selector_ran": selector is not None,
             "selector_ok": bool((selector or {}).get("ok")), "agent_state": res.get("agent_state")}
    if res.get("status") == "needs_human" or res.get("agent_state") == "needs_human":
        return verdict("needs_human", detail=str(res.get("needs_human_reason") or ""), **state)
    if not res.get("connected"):
        return verdict("offline", **state)
    if res.get("mode") != "agent":
        return verdict("not_agent_mode", **state)
    if selector is None:
        return verdict("selector_not_run", **state)
    if not state["selector_ok"]:
        return verdict("selector_failed", **state)
    return verdict("ready", **state)


async def manager_activity(db: AsyncSession) -> dict[str, Any] | None:
    """What the manager is doing right now (filled in by the manager runtime)."""
    return None
