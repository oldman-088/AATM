"""R6 — the operations guide documents Arena Agent mode, and only settings that exist."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
REMOVED = ("ARENA_AUTOMATION_APPROVED", "ARENA_APPROVAL_REFERENCE", "ARENA_DOMAINS", "ARENA_MIN_REQUEST_INTERVAL_SECONDS",
           "ARENA_DAILY_CAP_PER_PROFILE")


def _section() -> str:
    start = DOC.index("## وضع وكيل Arena (التشغيل الكامل)")
    end = DOC.index("\n## ", start + 10)
    return DOC[start:end]


def test_arena_agent_operations_section_covers_the_required_topics():
    section = _section()
    for needle in ("ملف Chrome", "سجّل الدخول إلى Arena", "GitHub", "Memory Saver", "Tampermonkey", "تدوير السكربت",
                   "اختبار المحددات", "لا تخمّن المحددات", "ابدأ **بملف واحد ومهمة صغيرة واحدة**", "وليست قيداً دائماً",
                   "استئناف الجلسة", "20 دقيقة", "gh auth", "[AATM-<task_id>]", "agent.code", "new_session"):
        assert needle in section, needle
    for problem in ("انقطاع المتصفح", "فشل اتصال السكربت", "محددات مفقودة", "استعادة الجلسة", "فشل الإرسال",
                    "مصادقة GitHub", "عدم اكتشاف طلب الدمج", "جلسة متوقفة"):
        assert problem in section, problem


def test_documented_arena_settings_exist_and_removed_ones_are_not_documented():
    for name in REMOVED:
        assert name not in DOC
    documented = set(re.findall(r"\b(ARENA_[A-Z_]+)=", _section()))
    assert {"ARENA_CONNECTOR_ENABLED", "ARENA_ALLOWED_ORIGINS", "ARENA_AGENT_NEW_SESSION_URL", "ARENA_AGENT_MAX_KEEP_WORKING"} <= documented
    fields = set(Settings.model_fields)
    bridge_only = {"ARENA_BRIDGE_TOKEN"}  # read from the environment by the backend credential policy and the bridge
    assert documented - fields - bridge_only == set()
