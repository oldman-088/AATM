"""Human checks and Arena's tool-call limit, on the mock Arena page and in the runner's decision."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bridge.connector import (
    DEFAULT_DOM,
    DEFAULT_OFFLINE_PHRASES,
    DEFAULT_OFFLINE_SELECTORS,
    DEFAULT_STOP_PHRASES,
    DEFAULT_STOP_SELECTORS,
)

from app.browser.arena_connector import SCRIPT_VERSION, USERSCRIPT_BODY
from app.browser.manager import detect_playwright_capabilities
from app.orchestration.arena_runner import ArenaAgentRunner

MOCK_PAGE = Path(__file__).resolve().parents[2] / "bridge" / "mock_arena" / "index.html"
ORIGIN = "http://mock.test"
needs_chromium = pytest.mark.skipif(
    not detect_playwright_capabilities()["chromium_installed"], reason="Playwright Chromium is not installed"
)
FAKE_SOCKET = """
window.__sent = [];
window.WebSocket = class {
  constructor() { this.readyState = 1; window.__socket = this; setTimeout(() => this.onopen && this.onopen(), 0); }
  send(raw) {
    const msg = JSON.parse(raw);
    window.__sent.push(msg);
    if (msg.type === 'hello') this.onmessage({ data: JSON.stringify(window.__config) });
  }
  close() {}
};
"""


@pytest.fixture
async def open_page():
    playwright_api = pytest.importorskip("playwright.async_api")
    config = {"type": "config", "profile_id": "p1", "dom": DEFAULT_DOM, "stop_selectors": DEFAULT_STOP_SELECTORS,
              "stop_phrases": DEFAULT_STOP_PHRASES, "offline_selectors": DEFAULT_OFFLINE_SELECTORS,
              "offline_phrases": DEFAULT_OFFLINE_PHRASES, "status": "ready", "mode": "agent",
              "reply_timeout_seconds": 5}
    script = USERSCRIPT_BODY.replace("__AATM_CONFIG__", json.dumps(
        {"profileId": "p1", "token": "t", "bridge": "ws://mock.test/ws", "version": SCRIPT_VERSION}))
    async with playwright_api.async_playwright() as playwright:
        executable = detect_playwright_capabilities().get("executable_path")
        browser = await playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        context = await browser.new_context()
        await context.route(f"{ORIGIN}/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=MOCK_PAGE.read_text(encoding="utf-8")))
        await context.add_init_script(FAKE_SOCKET + f"window.__config = {json.dumps(config)};")
        await context.add_init_script(script)

        async def _open(query: str):
            page = await context.new_page()
            await page.goto(f"{ORIGIN}/mock-arena/?{query}")
            await page.wait_for_function("window.__sent.some(m => m.type === 'state' || m.type === 'needs_human')")
            return page

        yield _open
        await browser.close()


async def _report(page) -> tuple[str | None, str | None]:
    sent = await page.evaluate("window.__sent")
    states = [m["state"] for m in sent if m["type"] == "state"]
    human = [m["reason"] for m in sent if m["type"] == "needs_human"]
    return (states[-1] if states else None), (human[-1] if human else None)


@needs_chromium
@pytest.mark.parametrize("query", ["rate_text=1", "terms_link=1", "feedback=1"])
async def test_ordinary_page_text_is_not_a_human_check(open_page, query):
    state, human = await _report(await open_page(query))
    assert human is None and state not in {"needs_human", "tool_call_limit"}


@needs_chromium
@pytest.mark.parametrize(("query", "phrase"), [
    ("rate_notice=1", "rate limit"), ("rate_inline=1", "rate limit"), ("terms_dialog=1", "terms of service"),
])
async def test_real_notices_still_stop_the_session(open_page, query, phrase):
    _state, human = await _report(await open_page(query))
    assert human == f"page shows: {phrase}"


@needs_chromium
async def test_tool_call_limit_is_its_own_state_and_one_message_clears_it(open_page):
    page = await open_page("tool_limit=1")
    assert await _report(page) == ("tool_call_limit", None)
    await page.evaluate("""window.__socket.onmessage({ data: JSON.stringify({
        type: 'command', request_id: 'r1', command: 'submit', args: { text: 'continue' } }) })""")
    await page.wait_for_function("window.__sent.some(m => m.type === 'result' && m.request_id === 'r1')")
    result = await page.evaluate("window.__sent.find(m => m.type === 'result')")
    assert result["ok"] is True, result
    assert result["data"]["sent"] is True
    assert await page.locator("[data-role='user']").all_inner_texts() == ["continue"]
    assert (await _report(page))[0] != "tool_call_limit"


@needs_chromium
async def test_captcha_wins_over_tool_call_limit_and_blocks_sending(open_page):
    page = await open_page("tool_limit=1&captcha=1")
    state, human = await _report(page)
    assert human and state != "tool_call_limit"
    await page.evaluate("""window.__socket.onmessage({ data: JSON.stringify({
        type: 'command', request_id: 'r1', command: 'submit', args: { text: 'continue' } }) })""")
    await page.wait_for_function("window.__sent.some(m => m.type === 'result' && m.request_id === 'r1')")
    assert (await page.evaluate("window.__sent.find(m => m.type === 'result')"))["code"] == "needs_human"
    assert await page.locator("[data-role='user']").count() == 0


async def _command(page, command: str, args: dict | None = None) -> dict:
    request_id = f"r-{command}"
    message = json.dumps({"type": "command", "request_id": request_id, "command": command, "args": args or {}})
    await page.evaluate("raw => window.__socket.onmessage({ data: raw })", message)
    await page.wait_for_function("id => window.__sent.some(m => m.type === 'result' && m.request_id === id)", arg=request_id)
    return await page.evaluate("id => window.__sent.find(m => m.type === 'result' && m.request_id === id)", request_id)


@needs_chromium
async def test_suggestion_chips_are_ignored(open_page):
    page = await open_page("suggestions=1&reply=The%20real%20agent%20reply")
    state, human = await _report(page)
    assert human is None and state == "ready"

    reply = await _command(page, "read_last_reply")
    assert reply["data"]["text"] == "The real agent reply"
    tested = await _command(page, "selector_test")
    assert tested["data"]["composer"]["found"] is True and tested["data"]["human_check"]["found"] is False

    await page.fill("#revise", "")
    sent = await _command(page, "submit", {"text": "continue"})
    assert sent["ok"] is True, sent
    assert await page.locator("[data-role='user']").all_inner_texts() == ["continue"]
    assert await page.evaluate("window.__chipClicks") == 0


@needs_chromium
async def test_suggestions_do_not_hide_a_real_human_check(open_page):
    _state, human = await _report(await open_page("suggestions=1&captcha=1"))
    assert human


def _runner(decision: str, sent_ok: bool = True):
    runner = ArenaAgentRunner.__new__(ArenaAgentRunner)
    calls = SimpleNamespace(sent=[], blocked=[])

    async def _decide(*_args):
        return decision, {}

    async def _follow_up(_db, _session, kind, text):
        calls.sent.append((kind, text))
        return sent_ok

    async def _block(_db, _session, _active, reason):
        calls.blocked.append(reason)

    runner._keep_working_decision, runner._follow_up, runner._block_needs_human = _decide, _follow_up, _block
    return runner, calls


class _Db:
    async def commit(self) -> None:
        return None


def _session(count: int = 0):
    return SimpleNamespace(keep_working_count=count, phase="working", events=[], updated_at=None)


async def test_mid_task_pause_sends_one_continue_and_counts_it():
    runner, calls = _runner("press")
    session = _session(1)
    handled, _ = await runner._handle_tool_call_limit(_Db(), session, None, None, None)
    assert calls.sent == [("tool_limit_continue", "continue")] and session.keep_working_count == 2
    assert handled == {"phase": "working", "keep_working": 2} and not calls.blocked


@pytest.mark.parametrize("decision", ["pr_exists", "final_report", "reply_unread", "github_unknown"])
async def test_finished_or_unknown_task_gets_no_message(decision):
    runner, calls = _runner(decision)
    session = _session()
    handled, _ = await runner._handle_tool_call_limit(_Db(), session, None, None, None)
    assert handled is None and not calls.sent and not calls.blocked and session.keep_working_count == 0


async def test_exhausted_continues_ask_the_owner():
    runner, calls = _runner("limit")
    await runner._handle_tool_call_limit(_Db(), _session(5), None, None, None)
    assert not calls.sent and "all 5 automatic continues" in calls.blocked[0]


async def test_failed_send_is_not_counted():
    runner, _calls = _runner("press", sent_ok=False)
    session = _session(2)
    await runner._handle_tool_call_limit(_Db(), session, None, None, None)
    assert session.keep_working_count == 2
