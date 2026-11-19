"""Arena browser connector (backend side).

The human signs in to Arena manually in each employee's own Chrome profile and installs the
userscript generated here (after reviewing it). The userscript talks only to the local bridge
(``ws://127.0.0.1:4100/v1/connector``) with a per-profile token. AATM never injects scripts
(``BrowserManager.run_userscript`` stays 501), never reads cookies/tokens, and never solves or
bypasses a human check: the profile is stopped as ``needs_human`` until a person resumes it.

Real Arena is used when ``ARENA_CONNECTOR_ENABLED`` is true; the userscript then runs on the
origins listed in ``ARENA_ALLOWED_ORIGINS``. There are no authorization references, expiry dates,
request intervals, usage quotas or profile-count limits: every employee profile with an installed
userscript is an independent Arena worker. ``ARENA_MOCK_URL`` (loopback only) allows full
end-to-end testing against the local mock page at any time.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.models import Agent, AIProvider, BrowserProfile

SCRIPT_VERSION = "2.2.0"
ARENA_BROWSER_PROVIDER_ID = "arena-browser"
ARENA_MODES = ("chat", "agent")
# R7: the manager's own Arena profile (Chrome --user-data-dir=$HOME/.aatm-profiles/manager).
MANAGER_AGENT_ID = "manager-0"
MANAGER_PROFILE_ID = "profile-manager"
MANAGER_PROFILE_NAME = "manager"
DOM_KEYS = ("input", "send", "assistant_message", "user_message", "busy", "agent_input", "agent_reply",
            "repo_control", "repo_option", "repo_search")
NOT_ENABLED_MESSAGE = (
    "Arena connector is not enabled: set ARENA_CONNECTOR_ENABLED=true (with ARENA_ALLOWED_ORIGINS), "
    "or set ARENA_MOCK_URL to test against the local mock page."
)


class ArenaNotEnabled(Exception):
    code = "not_enabled"


def _origin(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


def _is_loopback(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def configured_origins() -> list[str]:
    """Valid origins from ARENA_ALLOWED_ORIGINS (bare hosts are read as https)."""
    origins: list[str] = []
    for raw in (get_settings().ARENA_ALLOWED_ORIGINS or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        origin = _origin(raw if "://" in raw else f"https://{raw}")
        if origin:
            origins.append(origin)
    return list(dict.fromkeys(origins))


def real_arena_enabled() -> bool:
    return bool(get_settings().ARENA_CONNECTOR_ENABLED and configured_origins())


def arena_domains() -> list[str]:
    """Host names of the configured Arena origins (kept for callers that need a host)."""
    return [urlparse(o).netloc for o in configured_origins()]


def agent_new_session_url(real: bool) -> str | None:
    """Where a new Agent session starts. On real Arena this defaults to a full load of /agent: the
    "New Chat" link changes the page without a reload and leaves the old chat's messages in the DOM,
    where they would be read as the new session's reply."""
    explicit = (get_settings().ARENA_AGENT_NEW_SESSION_URL or "").strip()
    if explicit:
        return explicit
    origins = configured_origins() if real and not mock_url() else []  # the local mock keeps its New button
    return f"{origins[0]}/agent" if origins else None


def mock_url() -> str | None:
    url = (get_settings().ARENA_MOCK_URL or "").strip()
    # The mock must be a local page; a non-loopback "mock" would be a back door to a real site.
    return url if url and _origin(url) and _is_loopback(url) else None


def arena_gate() -> dict[str, Any]:
    s = get_settings()
    real = real_arena_enabled()
    mock = mock_url()
    mode = "real" if real else ("mock" if mock else "disabled")
    return {
        "mode": mode,
        "enabled": mode != "disabled",
        "real_enabled": real,
        "connector_enabled": bool(s.ARENA_CONNECTOR_ENABLED),
        "allowed_origins": configured_origins() if real else [],
        "mock_url": mock,
        "mock_url_rejected": bool((s.ARENA_MOCK_URL or "").strip()) and mock is None,
        "domains": arena_domains() if real else [],
        "agent_new_session_url": agent_new_session_url(real),
        "max_keep_working": s.ARENA_AGENT_MAX_KEEP_WORKING or None,  # None = unlimited
        "code": None if mode != "disabled" else "not_enabled",
        "message": None if mode != "disabled" else NOT_ENABLED_MESSAGE,
    }


def require_real_arena() -> None:
    """Guard for every real-Arena path (navigation, registration with real origins)."""
    if not real_arena_enabled():
        raise ArenaNotEnabled(NOT_ENABLED_MESSAGE)


def allowed_origins() -> list[str]:
    origins: list[str] = []
    mock = mock_url()
    if mock:
        origins.append(_origin(mock))  # type: ignore[arg-type]
    if real_arena_enabled():
        origins.extend(configured_origins())
    return [o for o in dict.fromkeys(origins) if o]


def match_patterns() -> list[str]:
    patterns: list[str] = []
    mock = mock_url()
    if mock:
        parsed = urlparse(mock)
        patterns.append(f"{_origin(mock)}{parsed.path.rstrip('/') or ''}/*")
    if real_arena_enabled():
        patterns.extend(f"{o}/*" for o in configured_origins())
    return list(dict.fromkeys(patterns))


def bridge_ws_url() -> str:
    base = (get_settings().ARENA_BRIDGE_URL or "http://127.0.0.1:4100").rstrip("/")
    return ("wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base[len("http://"):]) + "/v1/connector"


def mint_token(profile: BrowserProfile) -> str:
    """Create (or rotate) the per-profile userscript token. Only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    profile.connector_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    profile.connector_token_rotated_at = datetime.now(timezone.utc)
    return token


def build_userscript(profile: BrowserProfile, agent: Agent | None, token: str) -> str:
    """Generate the reviewable userscript for one profile. Minimal grants, scoped @match, no storage access."""
    patterns = match_patterns()
    if not patterns:
        raise ArenaNotEnabled(NOT_ENABLED_MESSAGE)
    header = "\n".join(
        [
            "// ==UserScript==",
            f"// @name         AATM Arena Connector - {profile.id}",
            "// @namespace    aatm.arena.connector",
            f"// @version      {SCRIPT_VERSION}",
            f"// @description  Connects this Arena tab to the local AATM bridge for {agent.name if agent else profile.id}: runs one prompt or Arena Agent command at a time, reports the session state, and stops on any human check.",
            "// @author       AATM (page automation adapted from arena-auto-continue by Anurag Kumar Nishad, MIT)",
            *[f"// @match        {p}" for p in patterns],
            "// @grant        none",
            "// @run-at       document-idle",
            "// @noframes",
            "// ==/UserScript==",
        ]
    )
    config = {
        "profileId": profile.id,
        "token": token,
        "bridge": bridge_ws_url(),
        "version": SCRIPT_VERSION,
    }
    return header + "\n\n" + USERSCRIPT_BODY.replace("__AATM_CONFIG__", json.dumps(config))


# The body only. Chat mode: types one prompt, waits for the complete answer and reports it.
# Agent mode: monitors the Arena Agent page (MutationObserver + 1 s tick) and runs the bridge's
# commands (new_session, submit, keep_working, read_last_reply, selector_test). Human-check and
# offline detection rules arrive from the bridge as data. It never touches cookies, storage,
# credentials or network APIs other than the one local WebSocket.
USERSCRIPT_BODY = r"""(function () {
  "use strict";
  // AATM Arena connector userscript (generated per employee profile).
  //
  // Page automation adapted from "Arena Auto Continue" by Anurag Kumar Nishad
  // (https://github.com/anuragkumarnishad/arena-auto-continue), MIT License,
  // Copyright (c) 2026 Anurag Kumar Nishad. See THIRD_PARTY_NOTICES.md in the AATM repository.
  // Adapted parts: element visibility check, composer detection, React-safe text insertion,
  // send-button scoring with form-submit / Enter fallbacks and send verification, stop-button and
  // "Keep working" detection, feedback-widget dismissal, composer cleanup after a failed send,
  // idle double-check, sleeping-tab detection and single-instance cleanup.
  //
  // Differences on purpose: AATM decides every action through the local bridge (no timers that
  // send messages on their own), the human-verification guard only stops and reports, and the
  // script talks to nothing but the one local WebSocket. It never reads site data or
  // credentials and has no randomised "human-like" timing.
  var CFG = __AATM_CONFIG__;
  if (window.self !== window.top) return;  // main frame only
  if (window.__aatmConnectorCleanup) { try { window.__aatmConnectorCleanup(); } catch (e) {} }

  var socket = null;
  var settings = null;
  var busy = false;      // chat mode: one prompt in flight
  var stopped = false;   // a human check was seen: nothing runs until the bridge resumes us
  var retryMs = 2000;
  var closed = false;
  var timers = [];
  var observer = null;
  var TOOL_LIMIT_SETTLE_MS = 15000;
  var IDLE_MS = 5000;    // "idle" = no Stop button for five seconds after generating
  var mon = { state: null, reason: null, busyVisible: false, lastBusyAt: 0, submittedAt: 0, prevTick: 0, dirty: false, newSessionAt: 0 };

  /* ---------------------------------------------------------------- DOM helpers */
  function q(sel) { try { return sel ? document.querySelector(sel) : null; } catch (e) { return null; } }
  function qa(sel) { try { return sel ? Array.prototype.slice.call(document.querySelectorAll(sel)) : []; } catch (e) { return []; } }
  function send(obj) { if (socket && socket.readyState === 1) socket.send(JSON.stringify(obj)); }
  function status(s, reason) { send({ type: "status", status: s, reason: reason || null }); }
  function notify(kind, detail) { send({ type: "event", kind: kind, detail: detail ? String(detail).slice(0, 200) : null }); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function textOf(el) { return ((el && (el.innerText || el.textContent)) || "").trim(); }
  function labelOf(el) {
    return ((el.getAttribute("aria-label") || "") + " " + (el.title || "") + " " + (el.getAttribute("data-testid") || "")).toLowerCase();
  }

  function isVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    var cs = window.getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && Number(cs.opacity) > 0.05;
  }

  // ---- Arena's optional follow-up suggestions (radio chips, Skip, "Revise options or write your own"):
  // noise. They are never clicked, typed into or read as the agent's reply.
  var SUGGESTION_BOX = /revise options|write your own/i;
  function fieldHint(el) {
    return ["placeholder", "aria-placeholder", "data-placeholder", "aria-label"].map(function (name) {
      return el.getAttribute(name) || "";
    }).join(" ");
  }
  function isSuggestionMarker(el) {
    if (el.matches("textarea, input, [contenteditable='true'], [role='textbox']")) return SUGGESTION_BOX.test(fieldHint(el));
    return /^skip$/i.test(textOf(el));
  }
  function suggestionPanels() {
    var panels = [];
    qa("[role='radio'], input[type='radio']").forEach(function (radio) {
      for (var node = radio.parentElement; node && node !== document.body; node = node.parentElement) {
        var parts = node.querySelectorAll("button, [role='button'], textarea, input, [contenteditable='true'], [role='textbox']");
        if (Array.prototype.some.call(parts, isSuggestionMarker)) {
          if (panels.indexOf(node) === -1) panels.push(node);
          return;
        }
      }
    });
    return panels;
  }
  function inSuggestionPanel(el, panels) {
    return (panels || suggestionPanels()).some(function (panel) { return panel.contains(el); });
  }

  // ---- merging happens only through AATM's Approval Center, never from the Arena page.
  function isMergeControl(el) {
    var text = textOf(el);
    return /^(?:squash and |rebase and )?merge(?:\s|$)/i.test(text) || /\bmerge\s+(?:pr|pull)\b/i.test(text + " " + labelOf(el));
  }
  function safeClick(el) {
    if (isMergeControl(el)) {
      notify("click_refused", "merge control");
      throw fail("merge_forbidden", "AATM never merges from the Arena page");
    }
    if (inSuggestionPanel(el)) throw fail("suggestion_forbidden", "suggestion options are never clicked");
    el.click();
  }
  function findMergeButton() {
    return qa("button, [role='button']").filter(function (el) { return isVisible(el) && isMergeControl(el); })[0] || null;
  }
  function findCreatePrButton() {
    return buttons().filter(function (el) { return isVisible(el) && /^create\s+(?:pr|pull request)$/i.test(textOf(el)); })[0] || null;
  }

  function buttons() {
    var panels = suggestionPanels();
    return qa("button, [role='button']").filter(function (el) { return !inSuggestionPanel(el, panels); });
  }

  function findInput() {
    var custom = settings && settings.dom && settings.dom.agent_input;
    if (custom) { var c = qa(custom).filter(isVisible); if (c.length) return c[c.length - 1]; }
    var selectors = ["textarea", "div[contenteditable='true']", "[role='textbox']"];
    var candidates = [];
    var panels = suggestionPanels();
    for (var i = 0; i < selectors.length; i++) {
      var els = qa(selectors[i]);
      for (var j = 0; j < els.length; j++) {
        if (SUGGESTION_BOX.test(fieldHint(els[j])) || inSuggestionPanel(els[j], panels)) continue;
        var rect = els[j].getBoundingClientRect();
        // Long transcripts can scroll the composer out of view: size + visibility decide, not the viewport.
        if (rect.width > 50 && rect.height > 10 && isVisible(els[j])) {
          candidates.push({ el: els[j], top: rect.top + window.scrollY });
        }
      }
    }
    if (!candidates.length) return null;
    candidates.sort(function (a, b) { return b.top - a.top; });  // the composer is the lowest box
    return candidates[0].el;
  }

  function findKeepWorking() {
    var els = buttons();
    for (var i = 0; i < els.length; i++) {
      if (/keep\s*working/i.test(els[i].textContent || "") && isVisible(els[i])) return els[i];
    }
    return null;
  }

  function findYesButton() {  // only detects the "Was this task successful?" panel; never clicked
    var els = buttons();
    for (var i = 0; i < els.length; i++) {
      if (/^yes$/i.test(textOf(els[i])) && isVisible(els[i])) return els[i];
    }
    return null;
  }

  function hasStopButton() {
    var busySel = settings && settings.dom && settings.dom.busy;
    if (busySel && q(busySel)) return true;
    var panels = suggestionPanels();
    var els = qa("button").filter(function (el) { return !inSuggestionPanel(el, panels); });
    for (var i = 0; i < els.length; i++) {
      var text = textOf(els[i]).toLowerCase();
      if ((isStopLabel(labelOf(els[i])) || /^stop( generating| response)?$/.test(text)) && isVisible(els[i])) return true;
    }
    return false;
  }

  // "stop" or "abort" as a whole word of a control name. A file name such as the changed-file chip
  // titled "scripts/ubuntu/stop.sh" is not a Stop button, so names with a path or a file extension
  // never count.
  function isStopLabel(label) {
    var parts = label.split(/\s+/);
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].replace(/[.,:;!?)]+$/, "");
      if (!part || /[\/\\.]/.test(part)) continue;
      if (/(^|[-_])(stop|abort)([-_]|$)/.test(part)) return true;
    }
    return false;
  }

  function findNewButton() {
    var els = qa("a, button, [role='button']");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!isVisible(el)) continue;
      var text = textOf(el);
      var label = labelOf(el);
      if (/^\+?\s*new(\s+(chat|session|task|agent|conversation))?$/i.test(text) ||
          /\bnew (chat|session|task|agent|conversation)\b/.test(label)) return el;
    }
    return null;
  }

  function findSendButton(inputEl, includeDisabled) {
    var form = inputEl.closest && inputEl.closest("form");
    var container = inputEl.closest && inputEl.closest("[class*='composer'], [class*='chat'], [class*='prompt']");
    var scopes = [];
    if (form) scopes.push(form);
    if (container && container !== form) scopes.push(container);
    scopes.push(document);
    var panels = suggestionPanels();
    function score(btn, inForm) {
      if ((btn.disabled && !includeDisabled) || !isVisible(btn) || inSuggestionPanel(btn, panels) || isMergeControl(btn)) return -1;
      var label = (labelOf(btn) + " " + btn.textContent).toLowerCase();
      if (/stop|abort/.test(label)) return -1;
      var s = 0;
      if (/send|submit/.test(label)) s += 6;
      if (btn.type === "submit") s += 4;
      if (btn.querySelector("svg")) s += 1;
      if (inForm) s += 1;
      return s;
    }
    for (var i = 0; i < scopes.length; i++) {
      var scope = scopes[i];
      var best = null, bestScore = 0;
      var list = scope.querySelectorAll("button");
      for (var j = 0; j < list.length; j++) {
        var sc = score(list[j], scope === form);
        if (sc > bestScore) { best = list[j]; bestScore = sc; }
      }
      if (best && bestScore >= (scope === document ? 6 : 4)) return best;
    }
    if (form) {
      var fb = form.querySelectorAll("button");
      for (var k = 0; k < fb.length; k++) {
        if ((includeDisabled || !fb[k].disabled) && isVisible(fb[k]) && !isMergeControl(fb[k])) return fb[k];
      }
    }
    return null;
  }

  function inputText(el) { return el.tagName === "TEXTAREA" || el.tagName === "INPUT" ? el.value : el.textContent; }

  // React-controlled inputs ignore a plain `.value =`: insertText (or the native setter plus an
  // input event) goes through the framework's own change tracking.
  function setInputText(el, text) {
    el.focus();
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      if (el.select) el.select();
    } else if (el.isContentEditable) {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    var ok = false;
    try { ok = text ? document.execCommand("insertText", false, text) : document.execCommand("delete", false); } catch (e) { ok = false; }
    if (!ok || inputText(el) !== text) {
      if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
        var proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (el.isContentEditable) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
      }
    }
  }

  function pressEnter(el) {
    ["keydown", "keypress", "keyup"].forEach(function (type) {
      el.dispatchEvent(new KeyboardEvent(type, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    });
  }

  function composerScope(input) {
    var scope = input;
    for (var i = 0; scope && scope.parentElement && i < 6; i++) {
      var p = scope.parentElement;
      if ((p.textContent || "").length > 3000) break;
      scope = p;
    }
    return scope || document;
  }

  function cleanComposer(input) {  // after a failed send: remove chips and clear the draft
    try {
      var list = composerScope(input).querySelectorAll("button, [role='button']");
      for (var i = 0; i < list.length; i++) {
        var lab = labelOf(list[i]);
        var txt = textOf(list[i]);
        if (/remove|delete/.test(lab) || ((txt === "×" || txt === "✕") && lab.length < 60)) { try { safeClick(list[i]); } catch (e) {} }
      }
      setInputText(input, "");
    } catch (e) {}
  }

  // Send button -> form.requestSubmit -> Enter, then verify the composer emptied (Enter retry).
  function attemptSend(input) {
    return new Promise(function (resolve) {
      var how;
      var btn = findSendButton(input);
      var form = input.closest && input.closest("form");
      if (btn && !btn.disabled) { safeClick(btn); how = "send_button"; }
      else if (form && form.requestSubmit) { try { form.requestSubmit(); how = "form_submit"; } catch (e) { pressEnter(input); how = "enter"; } }
      else { pressEnter(input); how = "enter"; }
      setTimeout(function () {
        if (!input.isConnected || inputText(input).trim() === "") { resolve({ ok: true, how: how }); return; }
        pressEnter(input);
        setTimeout(function () {
          var gone = !input.isConnected || inputText(input).trim() === "";
          resolve({ ok: gone, how: gone ? "enter_retry" : how });
        }, 700);
      }, 700);
    });
  }

  /* ---------------------------------------------------------------- page checks (data from the bridge) */
  function transcriptText() {
    if (!settings) return [];
    return qa(settings.dom.assistant_message).concat(qa(settings.dom.user_message)).concat(qa("[data-message-author-role]"))
      .concat(replyCandidates());
  }

  function pageTextWithoutTranscript() {
    var text = (document.body && document.body.innerText) || "";
    var transcript = transcriptText().concat(suggestionPanels());
    for (var j = 0; j < transcript.length; j++) { var t = transcript[j].innerText || ""; if (t) text = text.split(t).join(" "); }
    var input = findInput();
    if (input) { var v = inputText(input) || ""; if (v) text = text.split(v).join(" "); }
    return text.toLowerCase();
  }

  function humanCheckReason() {
    if (!settings) return null;
    for (var i = 0; i < settings.stop_selectors.length; i++) {
      var hit = qa(settings.stop_selectors[i]).filter(isVisible);
      if (hit.length) return "human check element on page";
    }
    var text = pageTextWithoutTranscript();
    for (var k = 0; k < settings.stop_phrases.length; k++) {
      var phrase = settings.stop_phrases[k];
      if (isTermsPhrase(phrase)) {
        // A "Terms of Use" link row is on every Arena page; only a real terms prompt stops the session.
        if (text.indexOf(phrase) !== -1 && termsPromptShown(phrase)) return "page shows: " + phrase;
      } else if (text.indexOf(phrase) !== -1 && noticeShown(phrase)) {
        return "page shows: " + phrase;
      }
    }
    return null;
  }

  // ---- other phrases: words on the page are not a check. The agent's tool output, file names and chat
  // titles can say "rate limit"; only a full-page block, a dialog / alert, or a short notice counts.
  var NOTICE_CONTAINER = "[role='alert'], [role='alertdialog'], [role='dialog'], [aria-modal='true'], dialog[open], [data-sonner-toast], [class*='toast' i]";
  var NOT_A_NOTICE = "pre, code, nav, aside, [class*='tool' i]";
  var RATE_PHRASE = /rate limit|too many requests|unusual traffic/;
  var RATE_WORDING = /\b(hit|reached|exceeded|try again|slow down|please wait|detected)\b/i;
  var NOTICE_MAX_CHARS = 300;

  function isBlockingPage() { return !findInput() && !findNewButton() && !findFeedbackPanel(); }

  function phraseElements(phrase) {  // the smallest visible elements holding the phrase, outside the transcript
    var skip = transcriptText().concat(suggestionPanels(), [findInput()]).filter(Boolean);
    var needle = phrase.split(" ")[0];
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var seen = [];
    for (var n = walker.nextNode(); n; n = walker.nextNode()) {
      if ((n.nodeValue || "").toLowerCase().indexOf(needle) === -1) continue;
      var el = n.parentElement;
      // The parent's text, so a phrase split across tags ("terms of <b>use</b>") still matches.
      while (el && el !== document.body && (el.textContent || "").toLowerCase().replace(/\s+/g, " ").indexOf(phrase) === -1) el = el.parentElement;
      if (!el || el === document.body || seen.indexOf(el) !== -1) continue;
      seen.push(el);
    }
    return seen.filter(function (el) {
      return isVisible(el) && !skip.some(function (s) { return s.contains(el); });  // not the agent's own words
    });
  }

  function insideModal(el) {
    for (var a = el; a && a !== document.body; a = a.parentElement) { if (isModalContainer(a)) return true; }
    return false;
  }

  function noticeShown(phrase) {
    if (isBlockingPage()) return true;
    var els = phraseElements(phrase);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.closest(NOTICE_CONTAINER) || insideModal(el)) return true;
      if (el.closest(NOT_A_NOTICE)) continue;
      var block = textOf(el).length > NOTICE_MAX_CHARS || !el.parentElement ? el : el.parentElement;
      var words = textOf(block);
      if (words.length > NOTICE_MAX_CHARS) words = textOf(el);
      if (words.length > NOTICE_MAX_CHARS) continue;
      if (!RATE_PHRASE.test(phrase) || RATE_WORDING.test(words)) return true;
    }
    return false;
  }

  // Arena's normal pause: not a human check. AATM's backend decides whether to send a message.
  function toolCallLimitShown() { return /tool[-\s]?call limit for this turn/.test(pageTextWithoutTranscript()); }

  // ---- terms prompts: the phrase counts only inside a dialog / modal / full-screen overlay, or next to
  // an Accept / Agree button. A plain "Terms of Use" link (a footer-style link row) is not a prompt.
  function isTermsPhrase(p) { return /\bterms\b/.test(p); }
  var ACCEPT_LABEL = /\b(accept|agree)\b/i;

  function isModalContainer(el) {
    if (el.tagName === "DIALOG" && el.open) return true;
    var role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "dialog" || role === "alertdialog" || el.getAttribute("aria-modal") === "true") return true;
    var cs = window.getComputedStyle(el);
    if (cs.position !== "fixed") return false;
    var r = el.getBoundingClientRect();
    return r.width >= window.innerWidth * 0.6 && r.height >= window.innerHeight * 0.6;  // full-screen overlay
  }

  function acceptButtonNear(start) {
    // Look a few levels up, but only inside a small block: an "Accept" elsewhere on the page doesn't count.
    var node = start;
    for (var depth = 0; node && node !== document.body && depth < 6; depth++, node = node.parentElement) {
      if (textOf(node).length > 1500) break;
      var btns = node.querySelectorAll ? node.querySelectorAll("button, [role='button'], input[type='submit'], input[type='button']") : [];
      for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        var label = (textOf(b) + " " + (b.value || "") + " " + labelOf(b));
        if (ACCEPT_LABEL.test(label) && isVisible(b)) return true;
      }
    }
    return false;
  }

  function termsPromptShown(phrase) {
    var els = phraseElements(phrase);
    for (var i = 0; i < els.length; i++) {
      if (insideModal(els[i]) || acceptButtonNear(els[i])) return true;
    }
    return false;
  }

  function offlineReason() {
    if (navigator.onLine === false) return "browser is offline";
    if (!settings) return null;
    var sels = settings.offline_selectors || [];
    for (var i = 0; i < sels.length; i++) { if (qa(sels[i]).filter(isVisible).length) return "disconnected banner on page"; }
    var phrases = settings.offline_phrases || [];
    var text = phrases.length ? pageTextWithoutTranscript() : "";
    for (var k = 0; k < phrases.length; k++) { if (text.indexOf(phrases[k]) !== -1) return "page shows: " + phrases[k]; }
    return null;
  }

  function replySelectors() {
    var list = [];
    if (settings && settings.dom && settings.dom.agent_reply) list.push(settings.dom.agent_reply);
    if (settings && settings.dom && settings.dom.assistant_message) list.push(settings.dom.assistant_message);
    return list.concat(["[data-message-author-role='assistant']", "[data-testid*='assistant' i]", "[class*='assistant-message' i]",
      "[class*='agent-message' i]", "[class*='prose' i]", "[class*='markdown' i]"]);
  }

  function replyCandidates() {
    var sels = replySelectors();
    var panels = suggestionPanels();
    for (var i = 0; i < sels.length; i++) {
      var els = qa(sels[i]).filter(function (el) {
        return textOf(el) && !(el.closest && el.closest("form")) && !inSuggestionPanel(el, panels);
      });
      if (els.length) { els.selector = sels[i]; return els; }
    }
    return [];
  }

  function latestReply() {
    var els = replyCandidates();
    if (!els.length) return { found: false, text: "", selector: null, count: 0 };
    var last = els[els.length - 1];
    return { found: true, text: textOf(last).slice(0, 200000), selector: els.selector, count: els.length };
  }

  /* ---------------------------------------------------------------- state monitor */
  function computeState() {
    var human = humanCheckReason();
    if (human) return { state: "needs_human", reason: human };
    var off = offlineReason();
    if (off) return { state: "offline", reason: off };
    if (!hasStopButton() && findMergeButton()) return { state: "pr_ready", reason: null };  // finished: goes to review
    // Right after a message the old notice may still be on the page: do not report it again yet.
    var settled = Date.now() - mon.submittedAt >= TOOL_LIMIT_SETTLE_MS;
    if (settled && !hasStopButton() && toolCallLimitShown()) return { state: "tool_call_limit", reason: null };
    if (findKeepWorking()) return { state: "keep_working_visible", reason: null };
    var now = Date.now();
    if (hasStopButton()) { mon.busyVisible = true; mon.lastBusyAt = now; return { state: "generating", reason: null }; }
    if (mon.busyVisible) { mon.busyVisible = false; mon.lastBusyAt = now; }  // the Stop button just went away
    // After a submit (the Stop button may not be rendered yet) or a generation, the page is only
    // "idle" once no Stop button was seen for five seconds (idle double-check).
    var lastActive = Math.max(mon.lastBusyAt, mon.submittedAt);
    if (lastActive && lastActive >= mon.newSessionAt) {
      return now - lastActive >= IDLE_MS ? { state: "idle", reason: null } : { state: "generating", reason: "settling" };
    }
    if (findInput()) return { state: "ready", reason: null };
    return { state: mon.state || "offline", reason: "composer not found" };
  }

  function evaluate(force) {
    if (!settings || closed) return;
    var now = Date.now();
    if (mon.prevTick && now - mon.prevTick > 90000) {
      notify("tab_slept", Math.round((now - mon.prevTick) / 60000) + " min without timers (keep the window visible, Memory Saver off)");
    }
    mon.prevTick = now;
    var s = computeState();
    if (s.state === "needs_human") {
      if (!stopped) { stopped = true; busy = false; send({ type: "needs_human", reason: s.reason }); }
      mon.state = "needs_human";
      return;
    }
    if (stopped) return;  // wait for the bridge to resume us after a human resolved it
    if (force || s.state !== mon.state) {
      mon.state = s.state;
      mon.reason = s.reason;
      send({ type: "state", state: s.state, reason: s.reason });
    }
  }

  function startMonitor() {
    if (observer || !window.MutationObserver) return;
    var pending = null;
    observer = new MutationObserver(function () {
      if (pending) return;
      pending = setTimeout(function () { pending = null; evaluate(false); }, 150);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, characterData: true });
    timers.push(setInterval(function () { evaluate(false); }, 1000));
    timers.push(setInterval(function () { send({ type: "ping" }); }, 30000));
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onNetwork);
    window.addEventListener("offline", onNetwork);
  }
  function onVisibility() { notify(document.visibilityState === "hidden" ? "tab_hidden" : "tab_visible"); }
  function onNetwork() { evaluate(true); }

  function waitFor(fn, timeoutMs, stepMs) {
    var started = Date.now();
    return new Promise(function (resolve) {
      (function check() {
        var v = null;
        try { v = fn(); } catch (e) { v = null; }
        if (v) { resolve(v); return; }
        if (Date.now() - started > timeoutMs) { resolve(null); return; }
        setTimeout(check, stepMs || 200);
      })();
    });
  }

  /* ---------------------------------------------------------------- Arena Agent commands */
  function fail(code, message) { var e = new Error(message); e.code = code; return e; }

  function pressEscape() {
    [document.activeElement, document.body, document].forEach(function (target) {
      if (!target || !target.dispatchEvent) return;
      ["keydown", "keyup"].forEach(function (type) {
        target.dispatchEvent(new KeyboardEvent(type, { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true }));
      });
    });
  }

  // ---- "Was this task successful? Yes / No / Keep working": it replaces the composer after a reply.
  // It is closed with its X or Escape. Yes and No are never clicked: that would rate the task.
  function findFeedbackPanel() {
    var starts = [findKeepWorking(), findYesButton()];
    for (var i = 0; i < starts.length; i++) {
      var node = starts[i];
      for (var depth = 0; node && node !== document.body && depth < 7; depth++, node = node.parentElement) {
        var text = node.textContent || "";
        if (text.length > 800) break;
        if (/was this task successful/i.test(text)) return node;
      }
    }
    return null;
  }

  function feedbackCloseButton(panel) {
    var list = Array.prototype.slice.call(panel.querySelectorAll("button, [role='button']")).filter(isVisible);
    var plain = list.filter(function (b) { return !/^(yes|no)$/i.test(textOf(b)) && !/keep\s*working/i.test(b.textContent || ""); });
    for (var i = 0; i < plain.length; i++) {
      if (/close|dismiss/.test(labelOf(plain[i])) || /^[×✕✖xX]$/.test(textOf(plain[i]))) return plain[i];
    }
    for (var j = 0; j < plain.length; j++) { if (!textOf(plain[j]) && plain[j].querySelector("svg")) return plain[j]; }
    return null;
  }

  async function closeFeedbackPanel() {
    var panel = findFeedbackPanel();
    if (!panel) return true;
    var closed = function () { return !findFeedbackPanel() && findInput(); };
    var btn = feedbackCloseButton(panel);
    if (btn) { safeClick(btn); if (await waitFor(closed, 2000, 100)) { notify("feedback_closed", "x"); return true; } }
    pressEscape();
    if (await waitFor(closed, 2000, 100)) { notify("feedback_closed", "escape"); return true; }
    return false;
  }

  // ---- repository picker: each Arena Agent chat works on the one repository selected under the composer.
  function normRepo(value) {
    return String(value || "").trim().toLowerCase().replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "");
  }
  function repoTextMatches(text, repo) {
    var got = normRepo(text), want = normRepo(repo);
    return !!got && !!want && (got === want || got === want.split("/").pop());
  }
  function firstLine(el) { return textOf(el).split("\n")[0].trim(); }

  function composerArea() {
    var input = findInput();
    if (!input) return null;
    var area = composerScope(input);
    for (var i = 0; i < 3 && area.parentElement && (area.parentElement.textContent || "").length <= 3000; i++) area = area.parentElement;
    return area;
  }
  function repoControls(area) {
    return Array.prototype.slice.call(area.querySelectorAll("button, [role='button'], [role='combobox'], [aria-haspopup]")).filter(isVisible);
  }

  function repoState(repo) {
    var custom = settings && settings.dom && settings.dom.repo_control;
    if (custom) {
      var chosen = qa(custom).filter(isVisible)[0];
      if (chosen) return { found: true, el: chosen, text: firstLine(chosen), via: "selector", matches: repoTextMatches(firstLine(chosen), repo) };
    }
    var area = composerArea();
    if (!area) return { found: false, el: null, text: null, via: null, matches: false };
    var list = repoControls(area), i;
    for (i = 0; i < list.length; i++) {
      if (repoTextMatches(firstLine(list[i]), repo)) return { found: true, el: list[i], text: firstLine(list[i]), via: "text", matches: true };
    }
    // A chat that already started can show its repository as plain text.
    var input = findInput(), leaves = area.querySelectorAll("span, div, p");
    for (i = 0; i < leaves.length; i++) {
      var leaf = leaves[i];
      if (leaf.children.length || leaf === input || input.contains(leaf) || !isVisible(leaf)) continue;
      if (repoTextMatches(textOf(leaf), repo)) return { found: true, el: null, text: textOf(leaf), via: "label", matches: true };
    }
    var found = null, via = null;
    for (i = 0; i < list.length && !found; i++) { if (/\brepo(sitory)?\b/.test(labelOf(list[i]))) { found = list[i]; via = "label"; } }
    for (i = 1; i < list.length && !found; i++) { if (/^arena\//i.test(firstLine(list[i]))) { found = list[i - 1]; via = "branch"; } }
    for (i = 0; i < list.length && !found; i++) {
      if (list[i].getAttribute("aria-haspopup") && /^[\w.-]+(\/[\w.-]+)?$/.test(firstLine(list[i]))) { found = list[i]; via = "popup"; }
    }
    return { found: !!found, el: found, text: found ? firstLine(found) : null, via: via, matches: false };
  }

  function describeControls() {
    var area = composerArea();
    return (area ? repoControls(area) : []).slice(0, 15).map(function (el) {
      return { tag: el.tagName.toLowerCase(), role: el.getAttribute("role"), popup: el.getAttribute("aria-haspopup"),
               text: firstLine(el).slice(0, 50), label: labelOf(el).trim().slice(0, 50) };
    });
  }

  function repoOptions() {
    var custom = settings && settings.dom && settings.dom.repo_option;
    var selector = custom || "[role='option'], [role='menuitem'], [role='menuitemradio'], [cmdk-item], [role='listbox'] button, " +
      "[role='menu'] button, [role='dialog'] button, [data-radix-popper-content-wrapper] button, [data-radix-popper-content-wrapper] [role='button']";
    return qa(selector).filter(isVisible);
  }
  function optionMatches(el, repo) {
    return textOf(el).split("\n").some(function (line) { return repoTextMatches(line, repo); });
  }
  function repoSearchBox() {
    var custom = settings && settings.dom && settings.dom.repo_search;
    var selector = custom || "[role='dialog'] input, [role='listbox'] input, [role='menu'] input, [cmdk-input], [data-radix-popper-content-wrapper] input";
    return qa(selector).filter(isVisible)[0] || null;
  }

  async function cmdSelectRepo(args) {
    var repo = String(args.repo || "");
    if (hasStopButton()) throw fail("busy", "the agent is still generating");
    if (!(await closeFeedbackPanel())) throw fail("feedback_not_closed", "the task feedback panel could not be closed");
    var state = repoState(repo);
    if (state.matches) return { selected: true, changed: false, repo: state.text };
    if (!state.el) return { selected: false, code: "repo_control_not_found", current: state.text, controls: describeControls() };
    var current = state.text;
    safeClick(state.el);
    await sleep(300);
    var search = repoSearchBox();
    if (search) { setInputText(search, repo.split("/").pop()); await sleep(300); }
    var option = await waitFor(function () { return repoOptions().filter(function (el) { return optionMatches(el, repo); })[0]; }, 6000);
    if (!option) {
      var seen = repoOptions().slice(0, 15).map(function (el) { return firstLine(el).slice(0, 60); });
      pressEscape();
      return { selected: false, code: "repo_not_available", current: current, options: seen };
    }
    safeClick(option);
    var done = await waitFor(function () { return repoState(repo).matches; }, 8000);
    if (!done) { pressEscape(); return { selected: false, code: "repo_not_selected", current: repoState(repo).text }; }
    evaluate(true);
    return { selected: true, changed: true, repo: repoState(repo).text };
  }

  async function cmdNewSession(args) {
    if (args.url) {
      var target = new URL(args.url, location.href);
      if (target.origin !== location.origin) throw fail("invalid_new_session_url", "new session URL must stay on this Arena origin");
      mon.newSessionAt = Date.now(); mon.lastBusyAt = 0; mon.submittedAt = 0; mon.busyVisible = false;
      setTimeout(function () { location.assign(target.href); }, 150);
      return { started: true, method: "url", navigating: true };
    }
    var btn = findNewButton();
    if (!btn) throw fail("new_button_not_found", "no New session button found on the page");
    var navigating = btn.tagName === "A" && btn.href && btn.target !== "_blank";
    mon.newSessionAt = Date.now(); mon.lastBusyAt = 0; mon.submittedAt = 0; mon.busyVisible = false;
    safeClick(btn);
    if (navigating) return { started: true, method: "button", navigating: true };
    await sleep(800);
    var input = await waitFor(function () { var i = findInput(); return i && !hasStopButton() && inputText(i).trim() === "" ? i : null; }, 20000);
    if (!input) throw fail("composer_not_found", "the new session did not show a composer");
    evaluate(true);
    return { started: true, method: "button", navigating: false, ready: true };
  }

  async function cmdSubmit(args) {
    var text = String(args.text || "");
    if (!text.trim()) throw fail("text_required", "nothing to submit");
    var reason = humanCheckReason();
    if (reason) { evaluate(true); throw fail("needs_human", reason); }
    if (hasStopButton()) throw fail("busy", "the agent is still generating");
    if (!(await closeFeedbackPanel())) throw fail("feedback_not_closed", "the task feedback panel could not be closed");
    var input = findInput();
    if (!input) throw fail("composer_not_found", "message composer not found (signed out or page changed)");
    if (input.disabled || input.readOnly) throw fail("composer_busy", "the composer is disabled");
    if (args.repo) {  // nothing is typed or sent unless this chat is on the task's repository
      var selected = repoState(args.repo);
      if (!selected.matches) {
        throw fail(selected.found ? "repo_mismatch" : "repo_unverified",
                   "selected repository is " + (selected.text || "unknown") + ", the task needs " + args.repo);
      }
    }
    try { input.scrollIntoView({ block: "center" }); } catch (e) {}
    setInputText(input, text);
    await sleep(300);
    if (inputText(input).trim() === "") throw fail("insert_failed", "the page did not accept the text");
    var res = await attemptSend(input);
    if (!res.ok) {
      cleanComposer(input);
      notify("send_failed", "text typed but not sent");
      throw fail("send_failed", "text was typed but the page did not send it");
    }
    mon.submittedAt = Date.now();
    evaluate(true);
    return { sent: true, how: res.how, chars: text.length };
  }

  async function cmdKeepWorking() {
    var btn = findKeepWorking();
    if (!btn) return { clicked: false, reason: "Keep working is not visible" };
    safeClick(btn);
    mon.submittedAt = Date.now();
    notify("keep_working_clicked");
    await sleep(300);
    evaluate(true);
    return { clicked: true };
  }

  function cmdReadLastReply() { return latestReply(); }

  async function cmdCloseFeedback() { return { closed: await closeFeedbackPanel() }; }

  function cmdCreatePr(args) {  // publishes this chat's work; AATM then checks GitHub and owns the PR
    var reason = humanCheckReason();
    if (reason) { evaluate(true); throw fail("needs_human", reason); }
    if (hasStopButton()) throw fail("busy", "the agent is still generating");
    var match = /^arena\/([A-Za-z0-9]{8})-/.exec(String(args.branch || ""));
    if (!match || location.pathname.slice(-8) !== match[1]) throw fail("branch_mismatch", "this chat is not the task's branch");
    var repo = args.repo ? repoState(args.repo) : null;
    if (repo && repo.found && /\//.test(repo.text || "") && !repo.matches) throw fail("repo_mismatch", "this chat shows another repository");
    var button = findCreatePrButton();
    if (!button) throw fail("create_pr_not_found", "Create PR is not on the page");
    var area = composerArea();
    var hasChanges = area && repoControls(area).some(function (el) { return /^\+\d[\d,]*(\s+-\d[\d,]*)?$/.test(firstLine(el)); });
    if (!hasChanges) throw fail("no_changes", "the page shows no changes to publish");
    safeClick(button);
    notify("create_pr_clicked", args.branch);
    return { clicked: true };
  }

  function cmdSelectorTest(args) {  // read-only: never clicks, types or sends
    args = args || {};
    var input = findInput();
    var sendBtn = input ? findSendButton(input, true) : null;  // disabled while the composer is empty
    var kw = findKeepWorking();
    var yes = findYesButton();
    var nb = findNewButton();
    var reply = latestReply();
    var human = humanCheckReason();
    var off = offlineReason();
    function describe(el) {
      if (!el) return { found: false };
      return { found: true, tag: el.tagName.toLowerCase(), label: (labelOf(el).trim() || textOf(el)).slice(0, 60), disabled: !!el.disabled };
    }
    var report = {
      composer: input ? { found: true, tag: input.tagName.toLowerCase(), editable: !!(input.isContentEditable || input.tagName === "TEXTAREA" || input.tagName === "INPUT"),
                          disabled: !!(input.disabled || input.readOnly), has_text: inputText(input).trim() !== "" } : { found: false },
      send: describe(sendBtn),
      stop: { found: hasStopButton() },
      keep_working: describe(kw),
      feedback_yes: describe(yes),
      feedback_panel: { found: !!findFeedbackPanel() },
      repo: (function () {
        var r = repoState(args.repo || "");
        return { found: r.found, text: r.text, via: r.via, matches: r.matches, controls: describeControls() };
      })(),
      new_session: describe(nb),
      latest_reply: { found: reply.found, selector: reply.selector, count: reply.count, chars: reply.text.length },
      human_check: { found: !!human, reason: human },
      offline: { found: !!off, reason: off },
      page: { origin: location.origin, path: location.pathname, title: document.title.slice(0, 120),
              visibility: document.visibilityState, online: navigator.onLine !== false },
      state: computeState().state,
      script_version: CFG.version
    };
    report.ok = !!(report.composer.found && (report.send.found || report.state !== "ready") && !report.human_check.found);
    return report;
  }

  var COMMANDS = { new_session: cmdNewSession, submit: cmdSubmit, keep_working: cmdKeepWorking,
                   read_last_reply: cmdReadLastReply, selector_test: cmdSelectorTest,
                   select_repo: cmdSelectRepo, create_pr: cmdCreatePr, close_feedback: cmdCloseFeedback };
  var READ_ONLY = { read_last_reply: true, selector_test: true };

  async function handleCommand(msg) {
    var fn = COMMANDS[msg.command];
    if (!fn) { send({ type: "result", request_id: msg.request_id, ok: false, code: "unknown_command", message: "unsupported command" }); return; }
    if (stopped && !READ_ONLY[msg.command]) {
      send({ type: "result", request_id: msg.request_id, ok: false, code: "needs_human", message: "stopped for a human" });
      return;
    }
    try {
      var data = await fn(msg.args || {});
      send({ type: "result", request_id: msg.request_id, ok: true, data: data || {} });
    } catch (err) {
      send({ type: "result", request_id: msg.request_id, ok: false, code: err.code || "command_failed", message: String(err.message || err).slice(0, 200) });
      if (err.code !== "needs_human") notify("error", (err.code || "command_failed") + ": " + String(err.message || err));
    }
  }

  /* ---------------------------------------------------------------- chat mode (one prompt -> one answer) */
  function typeInto(el, text) {
    el.focus();
    var proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) { desc.set.call(el, text); } else { el.value = text; }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function lastAnswer() {
    var all = qa(settings.dom.assistant_message);
    return { count: all.length, text: all.length ? (all[all.length - 1].innerText || "") : "" };
  }

  function stopForHuman(reason, requestId) {
    stopped = true;
    busy = false;
    send({ type: "needs_human", reason: reason, request_id: requestId || null });
  }

  function handlePrompt(msg) {
    if (stopped) { send({ type: "error", request_id: msg.request_id, code: "needs_human", message: "stopped for a human" }); return; }
    if (busy) { send({ type: "error", request_id: msg.request_id, code: "busy", message: "one request at a time" }); return; }
    var reason = humanCheckReason();
    if (reason) { stopForHuman(reason, msg.request_id); return; }
    var input = q(settings.dom.input);
    var button = q(settings.dom.send);
    if (!input || !button) { stopForHuman("chat input not found (page changed or signed out)", msg.request_id); return; }
    busy = true;
    status("busy");
    var before = lastAnswer().count;
    typeInto(input, msg.prompt);
    setTimeout(function () { safeClick(button); }, 150);
    var started = Date.now();
    var lastText = null;
    var stableSince = 0;
    var timeoutMs = (settings.reply_timeout_seconds || 600) * 1000;
    var timer = setInterval(function () {
      var r = humanCheckReason();
      if (r) { clearInterval(timer); stopForHuman(r, msg.request_id); return; }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer); busy = false; status("ready");
        send({ type: "error", request_id: msg.request_id, code: "timeout", message: "no complete answer in time" });
        return;
      }
      var a = lastAnswer();
      var generating = !!q(settings.dom.busy);
      if (a.count > before && !generating && a.text) {
        if (a.text === lastText) {
          if (Date.now() - stableSince >= 1000) {
            clearInterval(timer); busy = false;
            send({ type: "answer", request_id: msg.request_id, text: a.text });
            status("ready");
          }
        } else { lastText = a.text; stableSince = Date.now(); }
      }
    }, 250);
  }

  /* ---------------------------------------------------------------- bridge connection */
  function connect() {
    if (closed) return;
    socket = new WebSocket(CFG.bridge);
    socket.onopen = function () {
      retryMs = 2000;
      send({ type: "hello", profile_id: CFG.profileId, token: CFG.token, page_origin: location.origin, script_version: CFG.version });
    };
    socket.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "config") {
        settings = msg;
        stopped = msg.status === "needs_human";
        var reason = humanCheckReason();
        if (reason) { stopForHuman(reason); } else if (!stopped) { status("ready"); }
        if (msg.mode === "agent") { startMonitor(); mon.state = null; evaluate(true); }
      } else if (msg.type === "prompt") {
        handlePrompt(msg);
      } else if (msg.type === "command") {
        handleCommand(msg);
      } else if (msg.type === "resumed") {
        stopped = false;
        mon.state = null;
        evaluate(true);
      }
    };
    socket.onclose = function (ev) {
      socket = null;
      if (closed) return;
      // Never give up: a rejected page retries slowly, so it recovers when the bridge knows its token again.
      var rejected = ev.code === 4401 || ev.code === 4410;
      // 4409: a newer tab of this profile took over, so this one waits instead of fighting it.
      setTimeout(connect, ev.code === 4409 ? 300000 : (rejected ? 60000 : retryMs));
      if (!rejected) retryMs = Math.min(retryMs * 2, 30000);
    };
  }

  window.__aatmConnectorCleanup = function () {
    closed = true;
    timers.forEach(function (t) { clearInterval(t); });
    if (observer) observer.disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("online", onNetwork);
    window.removeEventListener("offline", onNetwork);
    if (socket) { try { socket.close(); } catch (e) {} }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", connect);
  else connect();
})();
"""


def _bridge_headers() -> dict[str, str]:
    import os

    token = os.environ.get("ARENA_BRIDGE_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _bridge_base() -> str:
    return (get_settings().ARENA_BRIDGE_URL or "http://127.0.0.1:4100").rstrip("/")


def profile_arena_mode(profile: BrowserProfile) -> str:
    mode = (getattr(profile, "arena_mode", None) or "chat").strip().lower()
    return mode if mode in ARENA_MODES else "chat"


async def register_profile_with_bridge(profile: BrowserProfile, *, dom: dict[str, str] | None = None) -> dict[str, Any]:
    """Tell the bridge which token hash / origins / mode this profile uses."""
    gate = arena_gate()
    if not gate["enabled"]:
        return {"ok": False, "code": "not_enabled", "message": NOT_ENABLED_MESSAGE}
    if not profile.connector_token_hash:
        return {"ok": False, "code": "no_token", "message": "Generate the profile's userscript first."}
    s = get_settings()
    body: dict[str, Any] = {
        "token_sha256": profile.connector_token_hash,
        "agent_id": profile.agent_id,
        "allowed_origins": allowed_origins(),
        "real_enabled": gate["real_enabled"],
        "mode": profile_arena_mode(profile),
        "new_session_url": agent_new_session_url(bool(gate["real_enabled"])),
    }
    configured_dom: dict[str, str] = {}
    raw_dom = (s.ARENA_DOM_SELECTORS or "").strip()
    if raw_dom:
        try:
            parsed = json.loads(raw_dom)
            if isinstance(parsed, dict):
                configured_dom = {str(k): str(v) for k, v in parsed.items() if k in DOM_KEYS}
        except ValueError:
            return {"ok": False, "code": "invalid_dom_selectors", "message": "ARENA_DOM_SELECTORS must be a JSON object."}
    merged_dom = {**configured_dom, **(dom or {})}
    if merged_dom:
        body["dom"] = merged_dom
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.put(f"{_bridge_base()}/v1/connector/profiles/{profile.id}", json=body, headers=_bridge_headers())
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "bridge_unreachable", "message": f"Local bridge unreachable: {type(exc).__name__}"}
    if res.status_code >= 400:
        detail = _detail(res)
        return {"ok": False, "code": detail.get("code", f"http_{res.status_code}"), "message": detail.get("message", "")}
    return {"ok": True, **res.json()}


async def bridge_call(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 10) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.request(method, f"{_bridge_base()}{path}", headers=_bridge_headers(), json=body)
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "bridge_unreachable", "message": f"Local bridge unreachable: {type(exc).__name__}"}
    if res.status_code >= 400:
        detail = _detail(res)
        return {"ok": False, "code": detail.get("code", f"http_{res.status_code}"), "message": detail.get("message", "")}
    return {"ok": True, **res.json()}


def _detail(res: httpx.Response) -> dict[str, Any]:
    try:
        detail = res.json().get("detail")
    except ValueError:
        return {}
    if isinstance(detail, dict):
        return detail
    return {"message": str(detail)[:300]} if detail else {}


async def ensure_arena_browser_provider(db: Any) -> AIProvider:
    """Create the ``arena-browser`` provider on first use (only once the connector is enabled).

    Several workers can reach this at the same moment, so the row is created in its own session
    and a concurrent duplicate insert is treated as success.
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    provider = await db.get(AIProvider, ARENA_BROWSER_PROVIDER_ID)
    if provider is not None:
        return provider
    async with AsyncSession(db.bind, expire_on_commit=False) as creator:
        creator.add(
            AIProvider(
                id=ARENA_BROWSER_PROVIDER_ID,
                name="Arena (browser connector)",
                provider_type="arena_browser",
                endpoint_url=get_settings().ARENA_BRIDGE_URL,
                credential_ref="env:ARENA_BRIDGE_TOKEN",
                default_model="arena_browser",
                available_models=["arena_browser"],
                priority=9,
                is_enabled=True,
                health_status="unknown",
                # Arena web usage records $0 provider cost; a 0 cap would block every call (spend >= cap).
                daily_limit_usd=1.0,
            )
        )
        try:
            await creator.commit()
        except IntegrityError:
            await creator.rollback()
    provider = await db.get(AIProvider, ARENA_BROWSER_PROVIDER_ID, populate_existing=True)
    assert provider is not None
    return provider
