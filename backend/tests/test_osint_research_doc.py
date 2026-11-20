from pathlib import Path


DOC = Path(__file__).resolve().parents[2] / "docs" / "osint-research.md"


def test_osint_research_has_required_projects_and_platform_matrix():
    text = DOC.read_text(encoding="utf-8")
    projects = [
        "sherlock-project/sherlock", "soxoj/maigret", "p1ngul1n0/blackbird",
        "WebBreacher/WhatsMyName", "qeeqbox/social-analyzer",
        "sundowndev/phoneinfoga", "megadose/holehe", "Maalfer/Toutatis",
        "Datalux/Osintgram", "twintproject/twint",
    ]
    assert sum(project in text for project in projects) >= 10
    for platform in ("WhatsApp", "Telegram", "Facebook", "Instagram", "X", "TikTok", "Snapchat"):
        assert f"| {platform} |" in text
    assert "Decision: what AATM should and should not implement" in text
    assert "CAPTCHA" in text and "official, documented APIs" in text
