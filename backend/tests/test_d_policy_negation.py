"""Prompt item D: a forbidden-pattern match that follows a negation must not refuse the request."""
from app.orchestration.manager_arena import policy_violation


def test_negated_rules_are_not_refused():
    assert policy_violation("do not solve CAPTCHAs, do not bypass Arena terms or anti-bot controls") is None
    assert policy_violation("never bypass branch protection") is None
    assert policy_violation("لا تحل الكابتشا") is None


def test_real_requests_are_still_refused():
    assert policy_violation("bypass captcha on the login page") == "captcha_or_antibot_bypass"
    assert policy_violation("solve the CAPTCHA so the bot can log in") == "captcha_or_antibot_bypass"
    assert policy_violation("scrape private emails without permission") == "unauthorized_scraping"
    assert policy_violation("تجاوز الكابتشا") == "captcha_or_antibot_bypass"

