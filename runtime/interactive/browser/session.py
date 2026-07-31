"""Persistent web-browser session — the backend for the single ``WebBrowser`` tool.

The browser sibling of the persistent ``terminal`` / ``python`` engines: instead
of a PTY-backed shell or a Jupyter kernel this keeps **one live Playwright
(Chromium) browser per Role session** that the model drives across calls:

  * open tabs, the navigated URLs, and the logged-in session (cookies /
    localStorage) persist between calls, so browsing state is built up step by
    step;
  * each action (navigate / click / type / read / screenshot / eval / back /
    tab management) runs against the live page and returns its result;
  * ``close`` shuts the browser down.

The live :class:`BrowserSession` is owned by the Role's ``RuntimeHost`` (one
implicit browser per session, like the ``terminal`` / ``python`` tools — there
is no model-facing browser id), so browsers are isolated per Role and share the
same lifecycle, revision, fencing, checkpoint, Surface, and Handoff machinery.
This module owns only the browser engine; ``BrowserRuntimeDriver`` adapts it to
the managed runtime contract.

Resume restore: :meth:`capture_state` snapshots the open-tab URLs (+ active tab)
and an optional ``storage_state`` (the logged-in session). :meth:`restore_state`
re-opens those tabs in a fresh browser seeded with that session — *without*
re-running any of the original navigation/click actions. Only page URLs +
storage are restored; live DOM state, scroll position, and in-flight JS are not.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlparse

try:
    from playwright.async_api import async_playwright
except ImportError as _playwright_import_error:  # optional browser integration
    async_playwright = None
else:
    _playwright_import_error = None

from mote.contracts.tool.errors import ToolError
from mote.runtime.media.html import html_to_markdown
from mote.runtime.presentation import count_noun, verb_agree
from mote.runtime.telemetry.logging import logger
from mote.runtime.text.elision import cap_head_tail

# --- Constants -------------------------------------------------------------
# Default per-action navigation timeout (ms). Playwright's own default is 30s;
# kept explicit so an action that hangs yields control rather than wedging.
DEFAULT_NAV_TIMEOUT_MS = 30_000
# Browser launch / readiness timeout (s).
_LAUNCH_TIMEOUT_S = 60.0
# Cap on extracted page text (chars), keeping head + tail, drop the middle.
# Set very high so full page text is returned intact in practice; the outer
# executor still persists oversized results to disk (recoverable) rather than
# dropping the middle here.
TEXT_MAX_CHARS = 10_000_000

# Cross-frame snapshot bounds. A page can nest arbitrarily many iframes (ad
# networks routinely inject dozens, several levels deep); walking every one is
# unbounded work + token blowup. We cap total frames traversed and nesting
# depth — the interactive login/consent forms that motivated iframe support sit
# one or two levels down, so a shallow cap keeps the common case while refusing
# to chase ad/tracker frame trees. Matches browser-use's ``max_iframe_depth``.
_MAX_FRAMES = 24
_MAX_FRAME_DEPTH = 5

# Complete model-facing message sentences, hoisted to module-top templates so the
# wording lives in one place (fill via ``.format(...)`` at the raise site).
_MSG_START_FAILED = "Error: web browser failed to start: {error}"
_MSG_NO_OPEN_TABS = "Error: the browser has no open tabs."
_MSG_NO_MATCH_SELECTOR_ERR = "Error: no element matched selector {selector!r}: {error}"
_MSG_NO_MATCH_SELECTOR = "Error: no element matched {selector!r}."
_MSG_SNAPSHOT_FAILED = "Error: failed to snapshot the page: {error}"
_MSG_WAIT_NEEDS_ONE = "Error: 'wait' needs exactly one of selector or expression."
_MSG_WAIT_TIMED_OUT = "Error: timed out after {timeout_ms}ms waiting for {target!r}{suffix}."
_MSG_DETECT_FORMS_FAILED = "Error: failed to detect forms: {error}"
_MSG_FILL_FORM_NEEDS_MAPPING = "Error: 'fill_form' needs a non-empty {selector: value} mapping."
_MSG_FILL_FAILED = "Error: failed to fill {target!r}: {error}"
_MSG_EXTRACT_NEEDS_MAPPING = "Error: 'extract' needs a non-empty {key: 'selector[@attr]'} mapping."
_MSG_EXTRACT_FAILED = "Error: failed to extract: {error}"
_MSG_NO_TAB_AT_INDEX = "Error: no tab at index {index} (have {count})."

# --- Stealth (opt-in anti-bot-detection) -----------------------------------
# Applied only when a session is created with ``stealth=True`` (Role opt-in via
# ``browser_schema.browser_stealth``). Off by default: the browser stays a plain
# headless Chromium. These measures defeat *passive* checks (the "HeadlessChrome"
# UA, the ``navigator.webdriver`` flag, a missing Accept-Language) — they do NOT
# solve active challenges (CAPTCHA / Cloudflare / DataDome); for those hand the
# managed browser runtime to a human.
#
# A realistic desktop Chrome UA replacing Playwright's headless default (which
# contains "HeadlessChrome" — an immediate bot tell).
_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Chromium launch flags that hide the most obvious automation signals.
_STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Chromium bakes ``--enable-automation`` in by default; drop it so the
# "Chrome is being controlled by automated test software" fingerprint is gone.
_STEALTH_IGNORE_DEFAULT_ARGS = ["--enable-automation"]
# Locale-independent context overrides (tier 1 + tier 2): a real desktop UA and
# a fixed viewport. The locale-dependent bits (locale / timezone / Accept-
# Language / navigator.languages) come from a ``_LOCALE_PROFILES`` bundle so they
# stay mutually consistent — see ``_resolve_locale`` / ``_context_kwargs``.
_STEALTH_VIEWPORT: Dict[str, int] = {"width": 1280, "height": 800}

# Coherent per-locale bundles. Each keeps locale + timezone + Accept-Language +
# navigator.languages internally consistent, so a bundle never contradicts
# itself. The chosen bundle should also match the exit-IP region (a zh-CN locale
# on a US IP is itself a bot tell) — this mirrors obscura's "match your proxy
# region" guidance; without a proxy we auto-pick from the host env (see
# ``_resolve_locale``), whose IP is the effective exit.
_LOCALE_PROFILES: Dict[str, Dict[str, Any]] = {
    "en": {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "accept_language": "en-US,en;q=0.9",
        "languages": ["en-US", "en"],
    },
    "zh": {
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "accept_language": "zh-CN,zh;q=0.9,en;q=0.8",
        "languages": ["zh-CN", "zh", "en"],
    },
}
# Fallback bundle when the requested one is unknown.
_DEFAULT_LOCALE = "en"

# Tmall's public pages expect a normal Chinese browser navigation.  Keep these
# compatibility headers scoped to Alibaba retail hosts rather than applying a
# misleading Referer to every site the agent visits.
_TMALL_HOST_SUFFIXES = ("tmall.com", "taobao.com")
_TMALL_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
_TMALL_REFERER = "https://www.tmall.com/"


def _tmall_compatible_headers(url: str, headers: Dict[str, str], *, is_navigation: bool) -> Dict[str, str]:
    """Return Tmall-only browser headers for one intercepted request."""
    hostname = (urlparse(url).hostname or "").lower()
    if not any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in _TMALL_HOST_SUFFIXES):
        return headers

    compatible = dict(headers)
    compatible["accept-language"] = _TMALL_ACCEPT_LANGUAGE
    if is_navigation:
        compatible["referer"] = _TMALL_REFERER
    return compatible


def _resolve_locale(value: str) -> str:
    """Resolve a ``browser_locale`` setting to a concrete bundle key.

    ``"en"`` / ``"zh"`` pass through. ``"auto"`` (or anything unknown) infers the
    region from the host's locale env vars (``LC_ALL`` / ``LC_CTYPE`` / ``LANG``
    / ``LANGUAGE``): a Chinese host → ``"zh"``, else ``"en"``. Absent a proxy the
    host is the effective exit IP, so this keeps the fingerprint region-coherent.
    """
    key = (value or "").strip().lower()
    if key in _LOCALE_PROFILES:
        return key
    for var in ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"):
        env = os.environ.get(var, "")
        if env and "zh" in env.lower():
            return "zh"
    return _DEFAULT_LOCALE


def _parse_proxy(value: str) -> Optional[Dict[str, Any]]:
    """Parse a proxy URL string into Playwright's ``proxy`` launch dict.

    Accepts a single URL string like ``http://host:port``,
    ``http://user:pass@host:port``, or ``socks5://host:port``. A bare
    ``host:port`` (no scheme) is treated as ``http://``. Playwright wants the
    credentials split *out* of the server URL, so we return
    ``{"server": "<scheme>://<host>[:<port>]", "username": ..., "password": ...}``
    with the auth keys present only when the URL carries them. Empty/blank →
    ``None`` (no proxy). Unparseable (no host) → ``None`` so a typo silently
    disables the proxy rather than crashing the launch.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    proxy: Dict[str, Any] = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


# Injected into every new document *before* page scripts run (via
# ``context.add_init_script``). Papers over the properties headless Chromium
# leaves in an automated-looking state: the ``navigator.webdriver`` flag, an
# empty plugins/languages list, and the missing ``window.chrome`` object. The
# ``navigator.languages`` value is filled per-locale by ``_stealth_init_js`` so
# it agrees with the context's ``locale`` (a mismatch is itself a fingerprint).
_STEALTH_INIT_JS_TEMPLATE = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', { get: () => %(languages)s });
  } catch (e) {}
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
      });
    }
  } catch (e) {}
  try {
    if (!window.chrome) {
      window.chrome = { runtime: {} };
    }
  } catch (e) {}
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) {
      navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : orig(params);
    }
  } catch (e) {}
})();
"""


def _stealth_init_js(locale_key: str) -> str:
    """Build the stealth init script with the bundle's ``navigator.languages``."""
    profile = _LOCALE_PROFILES.get(locale_key, _LOCALE_PROFILES[_DEFAULT_LOCALE])
    return _STEALTH_INIT_JS_TEMPLATE % {"languages": json.dumps(profile["languages"])}


# Implicit ARIA role for a bare tag (no explicit ``role=``), used by the Tier-2
# _locate re-query to build a ``get_by_role`` locator from cached snapshot meta.
# Only the common interactive tags need entries; anything missing falls back to
# the cached name via ``get_by_text``.
_IMPLICIT_ROLE = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
    "summary": "button",
}


# --- Unified page tree ------------------------------------------------------
# A single page.evaluate that walks the DOM depth-first from ``document.body``
# and returns an *ordered* node list interleaving prose text nodes and
# interactive elements in reading order — the unified representation the model
# reads from ``snapshot``. This is the interactive-only listing's successor:
# the model gets both the clickable ``[N]`` refs AND the surrounding prose that
# tells it *what* to click, in one call, without hand-correlating two lists.
#
# Design (converges browser-use ``serialize_tree`` + agent-browser AX-tree):
#   * emitted nodes are only text nodes + interactive elements; non-interactive
#     wrapper tags (div/span/section/…) emit nothing but recurse their children,
#     so ``depth`` reflects *semantic* interactive nesting, not raw DOM depth;
#   * text inside an interactive element is folded into that element's
#     accessible ``name`` (not re-emitted as a separate text line);
#   * **containment collapse** (paint-order approximation without CDP): a nested
#     interactive element fully inside an already-emitted interactive ancestor
#     that adds no distinct accessible name is skipped (e.g. an icon-<span> with
#     ``cursor:pointer`` inside a <button>);
#   * refs are **stable within a page**: an element keeps its existing
#     ``data-agent-ref`` across re-snapshots; only genuinely new elements get a
#     fresh index counted up from the current ``maxRef`` (so the ``*`` is-new
#     marker in the serializer is meaningful).
#   * **visibility is two distinct questions** (see ``isRendered`` /
#     ``isActionable`` below): prose text uses strict *rendered* visibility
#     (opacity/size count), while genuine controls use Playwright's
#     *actionability* model which IGNORES opacity — so transparent custom-styled
#     controls (the classic "I agree to terms" checkbox whose real ``<input>`` is
#     drawn ``opacity:0``) still surface with a clickable ref. Only
#     ``display:none`` prunes a whole subtree; every other hidden state can be
#     overridden/bypassed by a descendant, so recursion continues past it.
#   * **the walk follows the FLATTENED (composed) tree** (see ``childrenOf``):
#     it descends OPEN shadow roots and resolves ``<slot>`` to its assigned
#     light-DOM nodes, so Web-Component login forms are not invisible. Playwright's
#     CSS engine already pierces open shadow DOM, so a ``data-agent-ref`` stamped
#     inside a shadow tree is directly clickable via ``wait_for_selector`` — no
#     click-path change is needed (the blocker hit-test is made shadow-aware in
#     ``_BLOCKER_JS``). CLOSED shadow roots are inaccessible to any script by
#     design.
#   * **cross-document ``<iframe>`` content IS traversed** (see
#     ``_collect_frames`` / ``snapshot``): the Same-Origin Policy makes a
#     cross-origin iframe's document unreadable from the top frame, so ``_TREE_JS``
#     cannot inline it. Instead the snapshot walks Playwright's ``page.frames``
#     (which unifies same- AND cross-origin frames into one Frame list, each with
#     a uniform ``evaluate``/locator API — Playwright hides the separate-CDP-target
#     juggling a raw-CDP client like browser-use must hand-roll). Each frame runs
#     the SAME ``_TREE_JS`` seeded with a session-wide monotonic ``refSeed`` so the
#     model sees ONE flat ``[N]`` namespace across all frames; a Python-side
#     ``_ref_frame`` map records which Frame owns each ref so ``click``/``type``/
#     ``wait`` resolve it against the right frame. Bounded by ``_MAX_FRAMES`` /
#     ``_MAX_FRAME_DEPTH`` to refuse ad/tracker frame-tree blowup.
#
# Returns ``{nodes: [...], viewportHeight, maxRef}`` where each node is either
# ``{kind:"text", depth, text}`` or
# ``{kind:"element", depth, ref, tag, role, type, name, value, placeholder,
#    href, checked, inViewport, bbox}``.
_TREE_JS = r"""
(refSeed) => {
  const INTERACTIVE_TAGS = new Set([
    'a','button','input','select','textarea','details','summary','option','label'
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button','link','menuitem','option','radio','checkbox','tab','textbox',
    'combobox','slider','spinbutton','search','searchbox','switch','menuitemcheckbox',
    'menuitemradio','treeitem'
  ]);
  const EVENT_ATTRS = ['onclick','onmousedown','onmouseup','onkeydown','onkeyup'];
  // Tags whose text content is machinery, not prose — never emit their text.
  const SKIP_TAGS = new Set(['script','style','noscript','template','head','title']);
  const TEXT_CAP = 200;

  // The snapshot answers TWO different questions and therefore needs TWO
  // visibility predicates — conflating them is what silently dropped functional
  // login controls (e.g. custom "I agree to terms" checkboxes):
  //
  //   * isRendered  — "would a HUMAN see this painted?"  Used for prose text and
  //     non-interactive containers. Strict: display:none, visibility:hidden/
  //     collapse, opacity:0, or a zero-size box all mean not-rendered.
  //
  //   * isActionable — "could the agent INTERACT with this?"  Used for genuine
  //     controls. Deliberately aligned with Playwright's own actionability
  //     model, which IGNORES opacity: the ubiquitous custom-styled control
  //     pattern draws the real <input>/<button> transparent (opacity:0) beneath
  //     a decorative visual, yet it is fully clickable. Only display:none,
  //     visibility:hidden/collapse, or a zero-size box truly remove interaction.
  //
  // The only difference between the two is the opacity test — but naming both
  // makes the intent (read vs. act) explicit and self-documenting.
  function displayNone(el) {
    return window.getComputedStyle(el).display === 'none';
  }

  function hiddenVisibility(el) {
    const v = window.getComputedStyle(el).visibility;
    return v === 'hidden' || v === 'collapse';
  }

  function zeroBox(el) {
    const r = el.getBoundingClientRect();
    return r.width === 0 && r.height === 0;
  }

  function isRendered(el) {
    if (displayNone(el) || hiddenVisibility(el)) return false;
    if (parseFloat(window.getComputedStyle(el).opacity || '1') === 0) return false;
    return !zeroBox(el);
  }

  function isActionable(el) {
    if (displayNone(el) || hiddenVisibility(el)) return false;
    return !zeroBox(el);  // opacity is intentionally ignored (Playwright does too)
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) {
      if (tag === 'a' && !el.getAttribute('href') && !el.onclick) {
        // bare <a> without href / handler is weak; fall through to role/tabindex
      } else {
        return true;
      }
    }
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    for (const a of EVENT_ATTRS) { if (el.hasAttribute(a)) return true; }
    const ti = el.getAttribute('tabindex');
    if (ti !== null && ti !== '-1') return true;
    if (el.isContentEditable) return true;
    try {
      if (window.getComputedStyle(el).cursor === 'pointer') return true;
    } catch (e) {}
    return false;
  }

  // A node with an INDEPENDENT strong interactive signal — a genuine control
  // (real interactive tag, explicit ARIA role, own inline handler, or
  // contentEditable) as opposed to weak/ambient interactivity inherited from a
  // clickable wrapper (cursor:pointer) or a stray tabindex. Strong controls are
  // never containment-collapsed as "decorative", so a real button/link nested
  // inside a clickable wrapper still gets its own [ref] to act on. Without this,
  // e.g. a "获取验证码" (get SMS code) button wrapped in a cursor:pointer div
  // whose innerText contains the button's label would be dropped entirely.
  function strongInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) {
      if (tag === 'a' && !el.getAttribute('href') && !el.onclick) {
        // bare <a> without href / handler is weak; fall through
      } else {
        return true;
      }
    }
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    for (const a of EVENT_ATTRS) { if (el.hasAttribute(a)) return true; }
    if (el.isContentEditable) return true;
    return false;
  }

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const ph = el.getAttribute('placeholder');
      if (ph && ph.trim()) return ph.trim();
      const val = el.value;
      if (val && String(val).trim()) return String(val).trim();
      const nm = el.getAttribute('name');
      if (nm && nm.trim()) return nm.trim();
    }
    if (tag === 'img') {
      const alt = el.getAttribute('alt');
      if (alt && alt.trim()) return alt.trim();
    }
    const txt = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    if (txt) return txt.slice(0, 160);
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return '';
  }

  function cleanText(s) {
    // Strip zero-width / BOM chars, collapse whitespace, trim.
    return s.replace(/[\u200B-\u200D\uFEFF]/g, '').replace(/\s+/g, ' ').trim();
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom,
             width: r.width, height: r.height };
  }

  // Child rect fully inside ancestor rect (1px tolerance for sub-pixel layout).
  function contained(child, anc) {
    return child.left >= anc.left - 1 && child.top >= anc.top - 1 &&
           child.right <= anc.right + 1 && child.bottom <= anc.bottom + 1;
  }

  // Flattened-tree children — the composed tree a user actually sees, crossing
  // Web-Component boundaries so shadow-DOM login forms are not invisible:
  //   * a shadow HOST (open shadow root) exposes its SHADOW tree, not its light
  //     children (light children are only rendered where a <slot> places them);
  //   * a <slot> exposes its assigned (light-DOM) nodes, falling back to its
  //     default content when nothing is slotted;
  //   * everything else exposes its ordinary childNodes.
  // Closed shadow roots (attachShadow({mode:'closed'})) return null shadowRoot
  // and are inaccessible to any script by design — a hard browser boundary.
  function childrenOf(node) {
    if (node.nodeType === 1) {
      const sr = node.shadowRoot;
      if (sr) return sr.childNodes;
      if (node.tagName.toLowerCase() === 'slot' &&
          typeof node.assignedNodes === 'function') {
        const assigned = node.assignedNodes({ flatten: true });
        if (assigned && assigned.length) return assigned;
      }
    }
    return node.childNodes;
  }

  // Stable numbering: keep an element's existing ref, else mint the next one.
  // The scan must pierce shadow roots too, or a ref stamped inside a shadow tree
  // would not raise maxRef and a new light-DOM element could be minted the SAME
  // number on the next snapshot — a silent ref collision breaking click/type.
  //
  // ``refSeed`` is the caller's cross-frame high-water mark: this frame's refs
  // are minted strictly ABOVE it so numbers never collide with another frame's
  // (each iframe is a separate document with its own data-agent-ref attributes,
  // which would otherwise all restart at 1). scanRefs can only RAISE maxRef, so
  // a frame's own previously-stamped refs are preserved across re-snapshots.
  let maxRef = refSeed || 0;
  (function scanRefs(root) {
    root.querySelectorAll('*').forEach((e) => {
      const a = e.getAttribute && e.getAttribute('data-agent-ref');
      if (a) { const v = parseInt(a, 10); if (!isNaN(v) && v > maxRef) maxRef = v; }
      if (e.shadowRoot) scanRefs(e.shadowRoot);
    });
  })(document);
  function stamp(el) {
    let ref = el.getAttribute('data-agent-ref');
    if (ref === null || ref === '') {
      maxRef += 1;
      ref = String(maxRef);
      el.setAttribute('data-agent-ref', ref);
    }
    return ref;
  }

  const vh = window.innerHeight || 0;
  const vw = window.innerWidth || 0;
  const nodes = [];

  // DFS. Parameters:
  //   ``anc``          — nearest emitted interactive-element info {el,name,rect}
  //                      (or null): while inside one, text is folded into its
  //                      name (not re-emitted) and nested interactive children
  //                      may be containment-collapsed.
  //   ``proseVisible`` — whether the ancestor chain is RENDERED for reading, so
  //                      text nodes under a hidden wrapper (opacity:0 /
  //                      visibility:hidden / zero-size) are not emitted as prose.
  //                      Controls are judged independently (isActionable), so an
  //                      actionable control under a non-rendered wrapper is still
  //                      discovered — we only gate PROSE on this flag.
  //
  // Subtree pruning: ONLY ``display:none`` (and SKIP_TAGS) prune the whole
  // subtree — that is the one state that definitively removes every descendant
  // from layout. Every other "hidden" state can be overridden or bypassed by a
  // descendant (a visibility:visible child under a visibility:hidden parent, an
  // actionable control under an opacity:0 wrapper, an overflowing child of a
  // zero-size box), so we must keep recursing and judge each node on its own
  // computed style.
  function walk(node, depth, anc, proseVisible) {
    if (node.nodeType === 3) {  // text node
      if (anc || !proseVisible) return;  // folded into ancestor, or not readable
      const t = cleanText(node.nodeValue || '');
      if (t.length > 1) {
        nodes.push({ kind: 'text', depth: depth, text: t.slice(0, TEXT_CAP) });
      }
      return;
    }
    if (node.nodeType !== 1) return;  // comments, etc.
    const tag = node.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return;
    if (displayNone(node)) return;  // sole subtree-pruning condition

    // A genuine control is judged by ACTIONABILITY (opacity ignored, matching
    // Playwright) so transparent custom-styled controls survive; everything else
    // is judged by strict RENDERED visibility. isInteractive is a superset of
    // strongInteractive (it also flags weak cursor:pointer / tabindex signals),
    // so weak/ambient clickables still require full rendered visibility and
    // never re-introduce opacity:0 decorative noise.
    const strong = strongInteractive(node);
    const showable = strong ? isActionable(node) : isRendered(node);

    if (showable && isInteractive(node)) {
      const name = accessibleName(node);
      const rect = rectOf(node);
      if (anc && contained(rect, anc.rect) &&
          (name === '' || anc.name.indexOf(name) !== -1) &&
          !strong) {
        // Decorative nested clickable (icon in a button, span in a link): do
        // not emit, but keep recursing at the same depth under the ancestor.
        // Exempt genuine controls (strongInteractive) — a real button/link
        // inside a clickable wrapper is a distinct action, not decoration.
        for (const child of childrenOf(node)) walk(child, depth, anc, proseVisible);
        return;
      }
      const ref = stamp(node);
      const inViewport = rect.bottom > 0 && rect.top < vh &&
                         rect.right > 0 && rect.left < vw;
      nodes.push({
        kind: 'element',
        depth: depth,
        ref: ref,
        tag: tag,
        role: (node.getAttribute('role') || '').toLowerCase(),
        type: (node.getAttribute('type') || '').toLowerCase(),
        name: name,
        value: node.value !== undefined ? String(node.value || '') : '',
        placeholder: node.getAttribute('placeholder') || '',
        href: node.getAttribute('href') || '',
        checked: (node.checked === true),
        inViewport: inViewport,
        bbox: [Math.round(rect.left), Math.round(rect.top),
               Math.round(rect.width), Math.round(rect.height)],
      });
      const childAnc = { el: node, name: name, rect: rect };
      for (const child of childrenOf(node)) walk(child, depth + 1, childAnc, proseVisible);
      return;
    }

    // Not emitted (non-interactive wrapper, or a hidden/non-actionable control):
    // recurse children at the SAME depth so indentation tracks semantic
    // (interactive) nesting, not raw DOM depth. Prose stays visible only while
    // this node is itself rendered — so text under a hidden wrapper is dropped,
    // yet actionable descendants beneath it remain discoverable.
    const childProse = proseVisible && isRendered(node);
    for (const child of childrenOf(node)) walk(child, depth, anc, childProse);
  }

  const root = document.body;
  if (root) {
    for (const child of childrenOf(root)) walk(child, 0, null, true);
  }
  return { nodes: nodes, viewportHeight: vh, maxRef: maxRef };
}
"""


# --- Blocker hit-test -------------------------------------------------------
# Given an element, hit-test its click point (center) via elementFromPoint and
# report what would actually receive the click — an overlay (consent banner,
# modal, sticky header) covering the target. Returns null when the click would
# land on the target (or a descendant/ancestor of it), else a short description
# of the blocker (tag#id.class). Mirrors agent-browser's blockerAt.
_BLOCKER_JS = r"""
(el) => {
  if (!el) return 'missing';
  const rect = el.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  // Shadow-aware hit test: document.elementFromPoint RETARGETS to the shadow
  // host for elements inside an open shadow root, so a shadow-DOM control would
  // otherwise be reported as "covered" by its own host. Descend shadow roots to
  // the real deep target, and walk ancestry across shadow boundaries (via the
  // root node's host) so host/descendant relationships resolve correctly.
  function deepHit(px, py) {
    let e = document.elementFromPoint(px, py);
    while (e && e.shadowRoot) {
      const inner = e.shadowRoot.elementFromPoint(px, py);
      if (!inner || inner === e) break;
      e = inner;
    }
    return e;
  }
  function stepUp(n) {
    if (n.parentElement) return n.parentElement;
    const r = n.getRootNode && n.getRootNode();
    return (r && r.host) || null;  // cross the shadow boundary to the host
  }
  function flatContains(a, b) {  // does a contain b in the flattened tree?
    for (let n = b; n; n = stepUp(n)) { if (n === a) return true; }
    return false;
  }
  let hit = deepHit(x, y);
  if (!hit) return null;  // off-screen; click() will scroll it in
  if (hit === el) return null;
  if (flatContains(el, hit)) return null;  // hit is a descendant of the target
  if (flatContains(hit, el)) return null;  // hit is an ancestor of the target
  const lab = hit.closest && hit.closest('label');
  if (lab && (lab.control === el || lab.contains(el))) return null;
  let desc = hit.tagName.toLowerCase();
  if (hit.id) desc += '#' + hit.id;
  else if (hit.className && typeof hit.className === 'string') {
    const cls = hit.className.trim().split(/\s+/).slice(0, 2).join('.');
    if (cls) desc += '.' + cls;
  }
  return desc;
}
"""


# --- Clean-HTML extraction (for Markdown conversion) ------------------------
# Return the main content as *cleaned HTML* rather than hand-building Markdown:
# strip noise containers (script/style/nav/footer/aside/svg/header/form
# controls) and hidden nodes, then hand the result to the ``markdownify``
# library in Python (see :func:`_html_to_markdown`). This fixes the old
# hand-rolled walker's "everything on one line" bug — the walker only emitted
# newlines for a whitelist of semantic tags, so div/section-based pages (most
# modern sites) collapsed into a single line. ``markdownify`` handles block
# spacing for all block-level elements.
#
# Noise/hidden nodes are marked with a temporary attribute on the LIVE DOM (so
# ``getComputedStyle`` visibility checks work), then the body is cloned, marked
# nodes are removed from the clone, and the clone's ``innerHTML`` is returned.
# The live DOM is left unchanged (markers are removed again immediately).
_CLEAN_HTML_JS = r"""
() => {
  const SKIP_TAGS = new Set([
    'script','style','noscript','svg','head','nav','footer','aside','header',
    'form','button','input','select','textarea','iframe','template'
  ]);
  function isHidden(el) {
    if (el.nodeType !== 1) return false;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return true;
    if (parseFloat(s.opacity || '1') === 0) return true;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return true;
    return false;
  }
  const root = document.body || document.documentElement;
  if (!root) return '';
  const marked = [];
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag) || isHidden(el)) {
      el.setAttribute('data-md-drop', '1');
      marked.push(el);
    }
  }
  const clone = root.cloneNode(true);
  clone.querySelectorAll('[data-md-drop]').forEach((n) => n.remove());
  // Restore the live DOM: drop the temporary markers we added above.
  for (const el of marked) el.removeAttribute('data-md-drop');
  return clone.innerHTML;
}
"""


def _html_to_markdown(
    html: str,
    *,
    extract_links: bool = False,
    extract_images: bool = False,
) -> Optional[str]:
    """Convert browser-cleaned HTML to Markdown via the shared purification kernel.

    This is a thin consumer-layer entry point: the browser-specific cleaning
    (the ``_CLEAN_HTML_JS`` visibility pass, run on the LIVE DOM before this is
    called) has already happened; here we only delegate to the one shared
    shared HTML-to-Markdown runtime projection,
    which every dirty-HTML source converges on. ``extract_links`` /
    ``extract_images`` pass straight through.

    Returns ``None`` if the kernel is unavailable or conversion fails, so
    :meth:`BrowserSession.read` can fall back to a plain-text dump.
    """
    return html_to_markdown(html, extract_links=extract_links, extract_images=extract_images)


# --- Form detection ---------------------------------------------------------
# Walk every <form> on the page and describe its fillable fields so the model
# can fill_form / submit in one shot. Each field carries a stable CSS selector
# (prefers #id, else name=, else nth-of-type) the model passes straight back to
# fill_form. Mirrors obscura's detect_forms. Returns
# ``{"forms": [{index, selector, name, action, method, submit,
# fields: [{selector, label, name, type, value, required, options}]}]}``.
_DETECT_FORMS_JS = r"""
() => {
  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const nm = el.getAttribute && el.getAttribute('name');
    if (nm) return el.tagName.toLowerCase() + '[name="' + CSS.escape(nm) + '"]';
    // nth-of-type fallback within parent
    const parent = el.parentElement;
    if (!parent) return el.tagName.toLowerCase();
    const tag = el.tagName.toLowerCase();
    const sibs = Array.from(parent.children).filter(c => c.tagName === el.tagName);
    const i = sibs.indexOf(el) + 1;
    return cssPath(parent) + ' > ' + tag + ':nth-of-type(' + i + ')';
  }
  function labelFor(el) {
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab && lab.innerText.trim()) return lab.innerText.trim();
    }
    const wrap = el.closest && el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const nm = el.getAttribute('name');
    return nm || '';
  }
  function fieldOf(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag)).toLowerCase();
    const f = {
      selector: cssPath(el),
      label: labelFor(el),
      name: el.getAttribute('name') || '',
      type: type,
      value: el.value !== undefined ? String(el.value || '') : '',
      required: el.required === true || el.getAttribute('aria-required') === 'true',
    };
    if (tag === 'select') {
      f.options = Array.from(el.options).map(o => o.value || o.text);
    }
    return f;
  }
  const out = [];
  const forms = Array.from(document.querySelectorAll('form'));
  forms.forEach((form, i) => {
    const controls = Array.from(form.querySelectorAll('input, textarea, select'))
      .filter(el => {
        const t = (el.getAttribute('type') || '').toLowerCase();
        return t !== 'hidden';
      });
    const submit = form.querySelector(
      'button[type="submit"], input[type="submit"], button:not([type])'
    );
    out.push({
      index: i,
      selector: cssPath(form),
      name: form.getAttribute('name') || form.getAttribute('id') || '',
      action: form.getAttribute('action') || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      submit: submit ? cssPath(submit) : '',
      fields: controls.map(fieldOf),
    });
  });
  return { forms: out };
}
"""


# --- Schema-driven extraction -----------------------------------------------
# Given a {key: "selector[@attr]"} schema, pull text (default) or an attribute
# (when the selector ends with ``@attr``) for each key. A selector matching
# multiple nodes returns a list; a single match returns a scalar; no match
# returns null. Mirrors obscura's schema-driven extract.
_EXTRACT_JS = r"""
(schema) => {
  function valueOf(el, attr) {
    if (attr) {
      if (attr === 'text') return (el.innerText || el.textContent || '').trim();
      if (attr === 'html') return el.innerHTML;
      const v = el.getAttribute(attr);
      if (v !== null) return v;
      // fall back to a DOM property (e.g. value/href/src resolved)
      return (el[attr] !== undefined && el[attr] !== null) ? String(el[attr]) : null;
    }
    return (el.innerText || el.textContent || '').trim();
  }
  const out = {};
  for (const key of Object.keys(schema)) {
    let spec = String(schema[key]);
    let attr = null;
    const at = spec.lastIndexOf('@');
    if (at > 0) { attr = spec.slice(at + 1); spec = spec.slice(0, at); }
    let nodes;
    try { nodes = Array.from(document.querySelectorAll(spec)); }
    catch (e) { out[key] = null; continue; }
    if (nodes.length === 0) { out[key] = null; }
    else if (nodes.length === 1) { out[key] = valueOf(nodes[0], attr); }
    else { out[key] = nodes.map(n => valueOf(n, attr)); }
  }
  return out;
}
"""


def _format_snapshot_line(el: Dict[str, Any]) -> str:
    """Render one interactive element as ``[N]<tag attrs>name``.

    Mirrors browser-use's serialization: the ``[N]`` index is what the model
    passes back to ``click``/``type``; a small whitelist of attributes
    (type/placeholder/checked/href) is shown, ``class`` and other noise omitted
    to keep the listing token-efficient. Shared by the unified-tree serializer
    (:func:`_format_tree`) so element lines render identically in both views.
    """
    ref = el.get("ref", "")
    tag = el.get("tag", "?")
    attrs = []
    typ = el.get("type")
    if typ:
        attrs.append(f'type="{typ}"')
    role = el.get("role")
    if role:
        attrs.append(f'role="{role}"')
    if el.get("checked"):
        attrs.append("checked")
    href = el.get("href")
    if href:
        # Truncate long hrefs so a single link can't blow up a line.
        href_s = href if len(href) <= 80 else href[:77] + "..."
        attrs.append(f'href="{href_s}"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    name = (el.get("name") or "").strip()
    # Inputs surface their placeholder as the label when there's no text.
    if not name and el.get("placeholder"):
        name = el["placeholder"]
    return f"[{ref}]<{tag}{attr_str}>{name}".rstrip()


def _format_tree(
    nodes: List[Dict[str, Any]],
    *,
    prev_refs: "frozenset[str] | set[str]" = frozenset(),
    interactive_only: bool = False,
) -> str:
    """Serialize the unified page tree (:data:`_TREE_JS` output) to text.

    Interleaves prose *text* nodes and clickable *element* lines by DOM reading
    order, indented by each node's semantic ``depth`` (two spaces per level).
    Element lines reuse :func:`_format_snapshot_line`; a leading ``*`` marks an
    element whose ``ref`` is not in *prev_refs* (i.e. new since the previous
    snapshot). ``interactive_only=True`` drops text nodes, emitting only the
    element lines (the same tree, filtered) for token-tight situations.
    """
    lines: List[str] = []
    for node in nodes:
        kind = node.get("kind")
        indent = "  " * int(node.get("depth", 0) or 0)
        if kind == "text":
            if interactive_only:
                continue
            text = (node.get("text") or "").strip()
            if text:
                lines.append(f"{indent}{text}")
        elif kind == "element":
            line = _format_snapshot_line(node)
            marker = "" if node.get("ref") in prev_refs else "*"
            lines.append(f"{indent}{marker}{line}")
    return "\n".join(lines)


def _ref_error(ref: str, meta: Dict[str, Dict[str, Any]]) -> str:
    """Build the actionable "re-snapshot" error for an unresolvable ``[N]`` ref.

    Pure string builder (no Playwright) so the wording is unit-testable. A ref
    the last snapshot knew about (``ref in meta``) means the DOM changed since;
    an unknown ref was never assigned. Either way the fix is a fresh snapshot.
    """
    known = ref in meta
    hint = "the page changed since the last snapshot" if known else f"no element [{ref}] in the last snapshot"
    return f"Error: element [{ref}] not found ({hint}). Take a fresh snapshot to get current element indices."


class BrowserSession:
    """One persistent Playwright Chromium browser owned by a Role session.

    Wraps the ``async_playwright`` context manager + a launched browser + a single
    browser context (so cookies/localStorage are shared across the session's
    tabs). Pages (tabs) live on the context; the active tab is tracked by index.
    """

    def __init__(
        self,
        *,
        session_key: str,
        cwd: Optional[str] = None,
        headless: bool = True,
        stealth: bool = False,
        browser_locale: str = "en",
        proxy: str = "",
        client_certs: Optional[List[Dict[str, Any]]] = None,
        cdp_endpoint: str = "",
    ) -> None:
        self.session_key = session_key
        self.cwd = cwd
        self.headless = headless
        # Opt-in anti-bot-detection (see the ``_STEALTH_*`` constants). Off by
        # default: a plain headless Chromium with no fingerprint overrides.
        self.stealth = stealth
        # Concrete locale bundle key ("en"/"zh") for the stealth fingerprint;
        # "auto" (or unknown) is resolved from the host env. Only consulted when
        # ``stealth`` is on. Defaults to "en" so pure construction is deterministic.
        self.locale = _resolve_locale(browser_locale)
        # Optional upstream proxy (one exit IP for the whole session). Parsed to
        # Playwright's launch ``proxy`` dict, or None when unset. Independent of
        # stealth, but pairs with it: the proxy's region should match ``locale``/
        # timezone so the fingerprint stays coherent with the exit IP.
        self.proxy = _parse_proxy(proxy)
        # Client TLS certificates for mutual-TLS logins (Playwright
        # ``client_certificates`` entries: origin + PEM/PKCS#12 paths +
        # passphrase). Applied to the context at creation. Ignored when attached
        # over CDP (the real browser owns its TLS config).
        self.client_certs = list(client_certs) if client_certs else []
        # CDP endpoint to attach to an already-running Chrome (empty = launch our
        # own). When set, ``start`` connects instead of launching, reuses the
        # existing context, and teardown only DISCONNECTS (never closes the
        # human's browser). ``_attached`` records which mode we are in.
        self.cdp_endpoint = cdp_endpoint or ""
        self._attached = False
        self._cm = None  # async_playwright() context manager
        self._pw = None  # the started Playwright object
        self._browser = None  # launched Browser
        self._context = None  # BrowserContext (shared cookies/storage)
        self._closed = False
        # Index of the active tab within ``self._context.pages``.
        self._active = 0
        # Last snapshot's ref metadata: {ref(str): {tag, role, name, ...}}.
        # Populated by snapshot(); used to give actionable errors and to re-query
        # (Tier-2 _locate) when a ref is acted on. Stamped onto the DOM as
        # data-agent-ref, so refs survive until the next snapshot or navigation.
        self._ref_meta: Dict[str, Dict[str, Any]] = {}
        # Which frame owns each ref: {ref(str): Playwright Frame}. A ref may live
        # in the main document or in any (same- or cross-origin) iframe; click/
        # type/wait must resolve the ``[data-agent-ref]`` selector against the
        # OWNING frame (Playwright selectors do not cross frame boundaries).
        # Populated per-frame by snapshot(); cleared by _invalidate_refs().
        self._ref_frame: Dict[str, Any] = {}
        # Session-wide monotonic ref high-water mark. Threaded into _TREE_JS as
        # ``refSeed`` so each frame mints refs strictly above every number used
        # so far this page — a single flat namespace across all frames with no
        # cross-frame collision even when frames are inserted between snapshots.
        # Reset to 0 by _invalidate_refs() on navigation (the whole document
        # tree, and all its data-agent-ref attributes, is replaced).
        self._ref_high_water: int = 0
        # Refs present in the *previous* snapshot (the ``*`` is-new diff
        # baseline). Refreshed by snapshot(), cleared by _invalidate_refs() on
        # navigation so a new page's refs all read as new.
        self._prev_refs: set = set()

    # --- lifecycle ---------------------------------------------------------

    async def start(self, *, storage_state: Optional[Dict[str, Any]] = None) -> None:
        """Launch the browser + a fresh context, optionally seeded with a session.

        ``storage_state`` (a Playwright ``{cookies, origins}`` dict) re-seeds the
        logged-in session on resume so re-opened tabs are already authenticated.
        """
        if async_playwright is None:  # pragma: no cover - depends on optional dep
            raise ToolError(
                "Error: the WebBrowser tool requires Playwright. Install it with "
                "`pip install playwright` and `playwright install chromium`."
            ) from _playwright_import_error
        try:
            self._cm = async_playwright()
            self._pw = await asyncio.wait_for(self._cm.start(), timeout=_LAUNCH_TIMEOUT_S)
            if self.cdp_endpoint:
                # Attach to an already-running Chrome instead of launching one.
                # We reuse its existing context (the human's logins / passkeys /
                # extensions), so stealth / proxy / storage_state seeding do NOT
                # apply — the real browser owns that config.
                self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
                self._attached = True
                contexts = list(self._browser.contexts)
                self._context = contexts[0] if contexts else await self._browser.new_context()
                # Ensure at least one tab so the model always has a page to act on.
                if not self._context.pages:
                    await self._context.new_page()
            else:
                self._browser = await self._pw.chromium.launch(**self._launch_kwargs())
                self._context = await self._browser.new_context(**self._context_kwargs(storage_state))
                # Under stealth, run the fingerprint patches before any page script.
                if self.stealth:
                    await self._context.add_init_script(_stealth_init_js(self.locale))
                await self._context.route("**/*", self._continue_with_tmall_headers)
                # Start with one blank tab so the model always has a page to act on.
                await self._context.new_page()
            self._active = 0
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            self.kill()
            raise ToolError(_MSG_START_FAILED.format(error=e))

    def _launch_kwargs(self) -> Dict[str, Any]:
        """Chromium ``launch`` kwargs; stealth adds the anti-automation flags.

        A configured proxy is attached at launch (browser-wide, one exit IP for
        the whole session) rather than per-context, independent of stealth.
        """
        kwargs: Dict[str, Any] = {"headless": self.headless}
        if self.stealth:
            kwargs["args"] = list(_STEALTH_LAUNCH_ARGS)
            kwargs["ignore_default_args"] = list(_STEALTH_IGNORE_DEFAULT_ARGS)
        if self.proxy:
            kwargs["proxy"] = dict(self.proxy)
        return kwargs

    def _context_kwargs(self, storage_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """``new_context`` kwargs; stealth adds the fingerprint overrides."""
        kwargs: Dict[str, Any] = {}
        if storage_state:
            kwargs["storage_state"] = storage_state
        if self.client_certs:
            kwargs["client_certificates"] = [dict(c) for c in self.client_certs]
        if self.stealth:
            profile = _LOCALE_PROFILES.get(self.locale, _LOCALE_PROFILES[_DEFAULT_LOCALE])
            kwargs.update(
                {
                    "user_agent": _STEALTH_USER_AGENT,
                    "viewport": dict(_STEALTH_VIEWPORT),
                    "locale": profile["locale"],
                    "timezone_id": profile["timezone_id"],
                    "extra_http_headers": {"Accept-Language": profile["accept_language"]},
                }
            )
        return kwargs

    async def _continue_with_tmall_headers(self, route: Any) -> None:
        """Apply narrowly scoped compatibility headers before a request continues."""
        request = route.request
        headers = _tmall_compatible_headers(
            request.url,
            dict(request.headers),
            is_navigation=request.is_navigation_request(),
        )
        await route.continue_(headers=headers)

    @property
    def closed(self) -> bool:
        return self._closed or self._browser is None

    # --- page helpers ------------------------------------------------------

    @property
    def _pages(self) -> list:
        """Live pages (tabs) on the context, in tab order."""
        if self._context is None:
            return []
        return list(self._context.pages)

    def _active_page(self):
        """Return the active page, clamping the index to a valid tab."""
        pages = self._pages
        if not pages:
            raise ToolError(_MSG_NO_OPEN_TABS)
        if self._active >= len(pages) or self._active < 0:
            self._active = len(pages) - 1
        return pages[self._active]

    # --- operations --------------------------------------------------------

    def _resolve_target(self, target: str) -> Tuple[str, Optional[str]]:
        """Turn a model-supplied *target* into a Playwright selector.

        Accepts either a snapshot index — ``"5"`` or ``"[5]"`` — which maps to
        ``[data-agent-ref="5"]`` (the attribute stamped by :meth:`snapshot`), or
        a raw CSS selector (anything else), used as-is for power users. Returns
        ``(selector, ref)`` where ``ref`` is the index string when *target* was
        an index, else ``None`` (so callers can give ref-specific errors).
        """
        t = (target or "").strip()
        inner = t[1:-1].strip() if t.startswith("[") and t.endswith("]") else t
        if inner.isdigit():
            return f'[data-agent-ref="{inner}"]', inner
        return t, None

    def _frame_for_ref(self, page, ref: Optional[str]):
        """Return the Playwright Frame that owns *ref* (else the main frame).

        A ``[N]`` ref may live in the main document or in any iframe; selectors do
        not cross frame boundaries, so click/type/wait must resolve against the
        frame that :meth:`snapshot` recorded in ``_ref_frame``. A raw CSS selector
        (``ref is None``) or an unknown/stale ref falls back to the main frame
        (raw selectors are a top-frame power-user path; a stale ref then fails
        through the normal Tier-2/Tier-3 "re-snapshot" error).
        """
        if ref is not None:
            frame = self._ref_frame.get(ref)
            if frame is not None:
                return frame
        return page.main_frame

    def _invalidate_refs(self) -> None:
        """Drop the snapshot ref state after a page change.

        The DOM ``data-agent-ref`` attributes die naturally when the page
        navigates (fresh document); this clears the Python-side mirrors — the
        ``_ref_meta`` used for Tier-2 re-query / actionable errors and the
        ``_prev_refs`` is-new diff baseline — so the next snapshot starts clean
        and its refs all read as new.
        """
        self._ref_meta = {}
        self._ref_frame = {}
        self._ref_high_water = 0
        self._prev_refs = set()

    async def navigate(self, url: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS) -> str:
        """Navigate the active tab to *url*; return a short status line."""
        page = self._active_page()
        await page.goto(url, timeout=timeout_ms)
        self._invalidate_refs()
        return f"[navigated to {page.url}] {await page.title()}"

    async def click(self, target: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS) -> str:
        """Click the element identified by *target* (``[N]`` index or selector).

        Before clicking we hit-test the element's center: if an overlay (consent
        banner, modal, sticky header) covers it, we fail early with what is in
        the way rather than clicking the wrong thing or timing out. A stale
        ``[N]`` index (DOM changed since the snapshot) gives a clear "re-snapshot"
        error.
        """
        page = self._active_page()
        selector, ref = self._resolve_target(target)
        frame = self._frame_for_ref(page, ref)
        handle = await self._locate(frame, selector, ref, timeout_ms)
        blocker = await self._blocker(frame, handle)
        if blocker:
            raise ToolError(
                f"Error: {target} is covered by <{blocker}> — dismiss/handle that "
                f"element first, then take a fresh snapshot and retry."
            )
        url_before = page.url
        await handle.click(timeout=timeout_ms)
        # A click that navigated (link / submit / router) invalidates the refs:
        # the new document's DOM has no data-agent-ref attributes. Hash-router
        # SPA transitions that don't change the URL won't invalidate here
        # (documented limitation) — Tier-2 role/name re-query recovers those.
        if page.url != url_before:
            self._invalidate_refs()
        return f"[clicked {target}] now at {page.url}"

    async def type_text(
        self,
        target: str,
        text: str,
        *,
        clear: bool = True,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> str:
        """Fill the field identified by *target* (``[N]`` index or selector).

        ``clear`` (default True) replaces the field's current value; set False to
        append to it.
        """
        page = self._active_page()
        selector, ref = self._resolve_target(target)
        frame = self._frame_for_ref(page, ref)
        handle = await self._locate(frame, selector, ref, timeout_ms)
        if clear:
            await handle.fill(text, timeout=timeout_ms)
        else:
            await handle.click(timeout=timeout_ms)
            await handle.type(text, timeout=timeout_ms)
        return f"[typed into {target}]"

    async def _locate(self, page, selector: str, ref: Optional[str], timeout_ms: int):
        """Return a Playwright ElementHandle for *selector*, resolving in tiers.

        *page* is the resolution context — a Playwright ``Page`` (main frame) OR
        a child ``Frame`` (when the ref lives in an iframe). Both expose the same
        ``wait_for_selector``/``get_by_role``/``get_by_text``/``evaluate`` API, so
        the tiers below work unchanged against whichever frame owns the ref.

        Three-tier resolution for ``[N]`` refs (raw CSS selectors use Tier 1
        only):

        * **Tier 1** — wait for the stamped ``[data-agent-ref="N"]`` attribute
          (the common case: same page, DOM intact).
        * **Tier 2** — the ref is known (``_ref_meta``) but its attribute is gone
          (the page re-rendered the same document, e.g. a client-side list
          refresh): re-query from the cached role/name via ``get_by_role`` (then
          ``get_by_text``); on a unique hit, re-stamp ``data-agent-ref`` onto it
          and proceed.
        * **Tier 3** — unresolvable: raise the actionable "re-snapshot" error.
        """
        try:
            handle = await page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
        except Exception as e:  # noqa: BLE001
            handle = None
            if ref is None:
                raise ToolError(_MSG_NO_MATCH_SELECTOR_ERR.format(selector=selector, error=e))
        if handle is not None:
            return handle
        if ref is None:
            raise ToolError(_MSG_NO_MATCH_SELECTOR.format(selector=selector))
        # Tier 2: ref known but attribute gone — re-query by cached role/name.
        meta = self._ref_meta.get(ref)
        if meta:
            handle = await self._requery_ref(page, ref, meta, timeout_ms)
            if handle is not None:
                return handle
        # Tier 3: give up with an actionable error.
        raise ToolError(_ref_error(ref, self._ref_meta))

    async def _requery_ref(self, page, ref: str, meta: Dict[str, Any], timeout_ms: int):
        """Tier-2 re-query: relocate *ref* by its cached role/name, then re-stamp.

        Returns an ElementHandle (with ``data-agent-ref`` freshly stamped back
        onto it) on a *unique* role/name (or text) hit, else ``None`` so the
        caller falls through to the Tier-3 error. Best-effort: any Playwright
        failure yields ``None``.
        """
        name = (meta.get("name") or "").strip()
        role = (meta.get("role") or "").strip()
        # Playwright's implicit ARIA role for the tag when no explicit role.
        if not role:
            role = _IMPLICIT_ROLE.get(meta.get("tag", ""), "")
        # Short budget: Tier-2 is a fallback, not the main path.
        budget = min(timeout_ms, 2000)
        candidates = []
        try:
            if role and name:
                candidates.append(page.get_by_role(role, name=name, exact=True))
            if name:
                candidates.append(page.get_by_text(name, exact=True))
        except Exception:  # noqa: BLE001 — locator construction is defensive
            return None
        for locator in candidates:
            try:
                if await locator.count() != 1:
                    continue
                handle = await locator.element_handle(timeout=budget)
            except Exception:  # noqa: BLE001 — try the next strategy
                continue
            if handle is None:
                continue
            try:
                await handle.evaluate("(el, r) => el.setAttribute('data-agent-ref', r)", ref)
            except Exception:  # noqa: BLE001 — re-stamp is advisory
                pass
            return handle
        return None

    async def _blocker(self, page, handle) -> Optional[str]:
        """Hit-test *handle*'s center; return the blocking element desc or None."""
        try:
            return await page.evaluate(_BLOCKER_JS, handle)
        except Exception:  # noqa: BLE001 — hit-test is advisory, never fatal
            return None

    async def read(self, *, extract_links: bool = False, extract_images: bool = False) -> str:
        """Return the active tab's main content as Markdown (capped).

        Extracts cleaned HTML (stripping chrome — nav/footer/script/forms/… —
        and hidden nodes) in the page, then converts it to Markdown with the
        ``markdownify`` library — far more agent-friendly than a raw innerText
        dump, and it lays out div/section based pages across proper lines
        instead of collapsing to one line.
        Falls back to body innerText (then raw HTML) if extraction or
        conversion fails on an unusual page / when ``markdownify`` is absent.

        By default images and hyperlink URLs are dropped (they dominate the
        output on most pages — decorative images and long percent-encoded query
        URLs). Set ``extract_links`` / ``extract_images`` to keep them when the
        model needs a URL to navigate to or an image src to inspect.
        """
        page = self._active_page()
        header = f"[{page.url}] {await page.title()}\n"
        try:
            html = await page.evaluate(_CLEAN_HTML_JS)
        except Exception:  # noqa: BLE001 — extraction may fail on exotic pages
            html = None
        if html and html.strip():
            md = _html_to_markdown(html, extract_links=extract_links, extract_images=extract_images)
            if md and md.strip():
                return header + cap_head_tail(md, TEXT_MAX_CHARS)[0]
        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001 — some pages have no body yet
            text = await page.content()
        return header + cap_head_tail(text, TEXT_MAX_CHARS)[0]

    async def snapshot(self, *, interactive_only: bool = False) -> str:
        """Return a unified indented tree of the page — prose + clickable refs.

        Runs :data:`_TREE_JS` on the active tab: a single DFS from ``document.body``
        that interleaves visible prose *text* and interactive *elements* in
        reading order, stamping each interactive element with a stable
        ``data-agent-ref`` ``[N]`` index. :func:`_format_tree` serializes that to
        an indented listing the model reads, then drives via ``click``/``type``
        with the same ``[N]`` index. Element refs are stored in ``self._ref_meta``
        (for Tier-2 re-query + actionable errors) and persist on the DOM until the
        next navigation. A leading ``*`` marks elements new since the previous
        snapshot. ``interactive_only=True`` drops the prose for a compact
        controls-only view of the same tree.
        """
        page = self._active_page()
        frames = self._collect_frames(page)
        # Thread a session-wide monotonic seed through every frame so the model
        # sees ONE flat [N] namespace across the main document and all iframes.
        seed = self._ref_high_water
        ref_meta: Dict[str, Dict[str, Any]] = {}
        ref_frame: Dict[str, Any] = {}
        sections: List[Tuple[Optional[str], List[Dict[str, Any]]]] = []
        main_failed = False
        for i, frame in enumerate(frames):
            try:
                data = await frame.evaluate(_TREE_JS, seed)
            except Exception as e:  # noqa: BLE001 — a detached/racing sub-frame is skipped
                if i == 0:
                    main_failed = e  # the main frame failing is a real error
                continue
            nodes = (data or {}).get("nodes", []) or []
            seed = max(seed, int((data or {}).get("maxRef", seed) or seed))
            for n in nodes:
                if n.get("kind") == "element" and n.get("ref"):
                    ref_meta[n["ref"]] = n
                    ref_frame[n["ref"]] = frame
            # Only surface a sub-frame section when it has content; the main
            # frame (i == 0) is always shown (it carries the page header).
            if i == 0 or any(nodes):
                label = None if i == 0 else self._frame_label(frame)
                sections.append((label, nodes))
        if main_failed:
            raise ToolError(_MSG_SNAPSHOT_FAILED.format(error=main_failed))
        self._ref_meta = ref_meta
        self._ref_frame = ref_frame
        self._ref_high_water = seed
        header = f"[{page.url}] {await page.title()}"
        body = self._format_sections(sections, interactive_only=interactive_only)
        # Refresh the is-new baseline for the next snapshot on this page.
        self._prev_refs = set(ref_meta)
        if not ref_meta and not body.strip():
            return f"{header}\n[no interactive elements found]"
        offscreen = sum(1 for el in ref_meta.values() if not el.get("inViewport", True))
        parts = [header]
        if body:
            parts.append(body)
        if offscreen:
            is_are = verb_agree(offscreen, "is", "are")
            parts.append(f"[{count_noun(offscreen, 'element')} {is_are} off-screen; scroll to bring into view]")
        return cap_head_tail("\n".join(parts), TEXT_MAX_CHARS)[0]

    def _collect_frames(self, page) -> List[Any]:
        """Return the page's frames to snapshot: main first, then descendants.

        Bounded by :data:`_MAX_FRAME_DEPTH` (nesting) and :data:`_MAX_FRAMES`
        (total), skipping detached frames. ``page.frames`` already flattens the
        whole frame tree (same- and cross-origin) in a stable order with the main
        frame first, so we simply filter it — Playwright abstracts away the
        separate-CDP-target handling a raw-CDP client would need for cross-origin
        (out-of-process) iframes.
        """
        frames: List[Any] = []
        try:
            all_frames = list(page.frames)
        except Exception:  # noqa: BLE001 — defensive; page may be closing
            main = getattr(page, "main_frame", None)
            return [main] if main is not None else []
        for frame in all_frames:
            try:
                if frame.is_detached():
                    continue
            except Exception:  # noqa: BLE001
                continue
            if self._frame_depth(frame) > _MAX_FRAME_DEPTH:
                continue
            frames.append(frame)
            if len(frames) >= _MAX_FRAMES:
                break
        return frames

    @staticmethod
    def _frame_depth(frame) -> int:
        """Nesting depth of *frame* (main frame == 0), walking parent links."""
        depth = 0
        try:
            parent = frame.parent_frame
            while parent is not None and depth <= _MAX_FRAME_DEPTH + 1:
                depth += 1
                parent = parent.parent_frame
        except Exception:  # noqa: BLE001 — treat unknowable lineage as deep
            return _MAX_FRAME_DEPTH + 1
        return depth

    @staticmethod
    def _frame_label(frame) -> str:
        """A short human/agent-facing tag for a sub-frame section header."""
        try:
            name = (frame.name or "").strip()
        except Exception:  # noqa: BLE001
            name = ""
        try:
            url = (frame.url or "").strip()
        except Exception:  # noqa: BLE001
            url = ""
        ident = name or url or "iframe"
        if len(ident) > 120:
            ident = ident[:117] + "…"
        return f"[frame: {ident}]"

    def _format_sections(
        self,
        sections: List[Tuple[Optional[str], List[Dict[str, Any]]]],
        *,
        interactive_only: bool,
    ) -> str:
        """Serialize main + per-frame node sections into one indented listing.

        The main frame's tree renders at the top level; each sub-frame's tree is
        introduced by its ``[frame: …]`` label and indented one level beneath it,
        so the model can tell which controls live inside an iframe (the SOP makes
        precise inline placement impossible for cross-origin frames, so grouping
        by frame is the honest, uniform representation).
        """
        chunks: List[str] = []
        for label, nodes in sections:
            tree = _format_tree(nodes, prev_refs=self._prev_refs, interactive_only=interactive_only)
            if label is None:
                if tree:
                    chunks.append(tree)
                continue
            if not tree.strip():
                continue
            indented = "\n".join(f"  {line}" for line in tree.split("\n"))
            chunks.append(f"{label}\n{indented}")
        return "\n".join(chunks)

    async def wait(
        self,
        *,
        selector: str = "",
        expression: str = "",
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> str:
        """Wait until a *selector* appears or a JS *expression* is truthy.

        Polls with exponential backoff (5ms → 200ms) up to ``timeout_ms`` — for
        dynamic content / SPA transitions where the next snapshot would otherwise
        race the DOM. Exactly one of ``selector`` / ``expression`` must be given.
        A ``selector`` resolves an ``[N]`` index too. Raises on timeout.
        """
        page = self._active_page()
        if bool(selector) == bool(expression):
            raise ToolError(_MSG_WAIT_NEEDS_ONE)
        if selector:
            sel, ref = self._resolve_target(selector)
            frame = self._frame_for_ref(page, ref)
            try:
                await frame.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            except Exception:  # noqa: BLE001
                raise ToolError(_MSG_WAIT_TIMED_OUT.format(timeout_ms=timeout_ms, target=selector, suffix=""))
            return f"[{selector} appeared]"
        # expression: poll to truthy with exponential backoff.
        deadline = time.monotonic() + timeout_ms / 1000.0
        delay = 0.005
        last_err = None
        while time.monotonic() < deadline:
            try:
                if await page.evaluate(f"Boolean({expression})"):
                    return f"[condition became true: {expression}]"
            except Exception as e:  # noqa: BLE001 — expression may throw mid-load
                last_err = e
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.2)
        suffix = f" (last error: {last_err})" if last_err else ""
        raise ToolError(_MSG_WAIT_TIMED_OUT.format(timeout_ms=timeout_ms, target=expression, suffix=suffix))

    async def detect_forms(self) -> str:
        """List the page's <form>s and their fillable fields.

        Returns a JSON description: each form has a ``selector`` + ``submit``
        selector and a ``fields`` list (each with a stable ``selector``, label,
        type, current value, required flag, and select ``options``). Feed those
        field selectors straight into :meth:`fill_form`. Mirrors obscura's
        detect_forms.
        """
        page = self._active_page()
        try:
            data = await page.evaluate(_DETECT_FORMS_JS)
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_DETECT_FORMS_FAILED.format(error=e))
        forms = (data or {}).get("forms", []) or []
        if not forms:
            return "[no forms found]"
        return json.dumps({"forms": forms}, ensure_ascii=False, indent=2)

    async def fill_form(
        self,
        fields: Dict[str, str],
        *,
        submit: str = "",
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> str:
        """Fill several fields in one call, then optionally submit.

        ``fields`` maps each target (a ``[N]`` index, a ``data-agent-ref`` index,
        or a raw CSS selector — e.g. from :meth:`detect_forms`) to the text to
        fill. Fields are filled in order; the first one that fails aborts with an
        actionable error naming it. When ``submit`` is given it is clicked after
        all fields are filled. Mirrors obscura's fill_form. Returns a summary.
        """
        if not isinstance(fields, dict) or not fields:
            raise ToolError(_MSG_FILL_FORM_NEEDS_MAPPING)
        page = self._active_page()
        filled = []
        for target, value in fields.items():
            selector, ref = self._resolve_target(str(target))
            handle = await self._locate(page, selector, ref, timeout_ms)
            try:
                await handle.fill(str(value), timeout=timeout_ms)
            except Exception as e:  # noqa: BLE001
                raise ToolError(_MSG_FILL_FAILED.format(target=target, error=e))
            filled.append(str(target))
        msg = f"[filled {count_noun(len(filled), 'field')}: {', '.join(filled)}]"
        if submit:
            sel, ref = self._resolve_target(submit)
            handle = await self._locate(page, sel, ref, timeout_ms)
            await handle.click(timeout=timeout_ms)
            msg += f" [submitted via {submit}] now at {page.url}"
        return msg

    async def extract(self, schema: Dict[str, str]) -> str:
        """Extract structured data by CSS selector, returning JSON.

        ``schema`` maps each output key to a CSS selector with an optional
        ``@attr`` suffix selecting an attribute (e.g. ``"a.title@href"``); with
        no suffix the element's trimmed text is taken. A selector matching
        multiple elements yields a list; one match yields a scalar; no match
        yields ``null``. Mirrors obscura's schema-driven extract. Returns a JSON
        object keyed by your schema keys.
        """
        if not isinstance(schema, dict) or not schema:
            raise ToolError(_MSG_EXTRACT_NEEDS_MAPPING)
        page = self._active_page()
        try:
            data = await page.evaluate(_EXTRACT_JS, schema)
        except Exception as e:  # noqa: BLE001
            raise ToolError(_MSG_EXTRACT_FAILED.format(error=e))
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def screenshot(self) -> bytes:
        """Capture the active tab as a PNG and return the raw bytes."""
        page = self._active_page()
        return await page.screenshot(type="png", full_page=False)

    async def focus(self) -> None:
        """Raise the active tab when this session owns a headed Chromium window."""
        await self._active_page().bring_to_front()

    async def handoff_pointer(self, x: float, y: float) -> None:
        """Deliver a human pointer click in viewport coordinates."""
        await self._active_page().mouse.click(x, y)

    async def handoff_drag(self, x: float, y: float, x2: float, y2: float) -> None:
        """Deliver a human pointer drag in viewport coordinates."""
        mouse = self._active_page().mouse
        await mouse.move(x, y)
        await mouse.down()
        await mouse.move(x2, y2, steps=12)
        await mouse.up()

    async def handoff_text(self, text: str) -> None:
        """Insert human text into the currently focused page control."""
        await self._active_page().keyboard.insert_text(text)

    async def handoff_key(self, key: str) -> None:
        """Deliver a human keyboard key to the active page."""
        await self._active_page().keyboard.press(key)

    async def read_image(self, target: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS) -> bytes:
        """Capture the image element identified by *target* as PNG bytes.

        *target* is a ``[N]`` snapshot index or a raw CSS selector, resolved by
        the same three-tier :meth:`_locate` used by ``click`` / ``type``. Unlike
        the full-tab :meth:`screenshot`, this renders just the one element — an
        ``<img>`` (or any node) — so a vision model reads only the picture the
        model asked about, not the whole page. Playwright rasterizes whatever the
        element currently displays, so it works for ``<img>``, ``<canvas>`` and
        CSS-background nodes alike, sidestepping cross-origin / hotlink-guard
        fetches of the raw ``src``.
        """
        page = self._active_page()
        selector, ref = self._resolve_target(target)
        handle = await self._locate(page, selector, ref, timeout_ms)
        return await handle.screenshot(type="png", timeout=timeout_ms)

    async def eval_js(self, expression: str) -> str:
        """Evaluate a JavaScript *expression* on the active tab; return its value.

        Playwright serializes the result to a Python object (dict/list/str/...).
        We render it as pretty JSON when it round-trips cleanly — far more
        agent-friendly than a Python ``repr`` — and fall back to ``repr`` for
        anything not JSON-serializable (e.g. a value carrying ``NaN``/circular
        structure that survived the bridge).
        """
        page = self._active_page()
        result = await page.evaluate(expression)
        try:
            return json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):  # noqa: BLE001 — non-JSON value
            return repr(result)

    async def back(self) -> str:
        """Navigate the active tab back in history."""
        page = self._active_page()
        await page.go_back()
        self._invalidate_refs()
        return f"[back] now at {page.url}"

    async def tabs(self) -> str:
        """List the open tabs (index, active marker, URL, title)."""
        lines: list[str] = []
        for i, page in enumerate(self._pages):
            marker = "*" if i == self._active else " "
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            lines.append(f"{marker} [{i}] {page.url} {title}".rstrip())
        return "\n".join(lines) if lines else "[no open tabs]"

    async def new_tab(self, url: Optional[str] = None) -> str:
        """Open a new tab (optionally navigating to *url*) and make it active."""
        assert self._context is not None, "browser not started"
        page = await self._context.new_page()
        self._active = len(self._pages) - 1
        # Refs belong to the tab that was active when the snapshot was taken;
        # switching the active page (even to a fresh tab) invalidates them.
        self._invalidate_refs()
        if url:
            await page.goto(url, timeout=DEFAULT_NAV_TIMEOUT_MS)
            return f"[opened tab {self._active}: {page.url}]"
        return f"[opened blank tab {self._active}]"

    async def switch_tab(self, index: int) -> str:
        """Make the tab at *index* active and bring it to the foreground."""
        pages = self._pages
        if index < 0 or index >= len(pages):
            raise ToolError(_MSG_NO_TAB_AT_INDEX.format(index=index, count=len(pages)))
        self._active = index
        await pages[index].bring_to_front()
        # The snapshot's refs were stamped on the previously-active page.
        self._invalidate_refs()
        return f"[switched to tab {index}: {pages[index].url}]"

    async def close_tab(self, index: int) -> str:
        """Close the tab at *index*, clamping the active index afterwards."""
        pages = self._pages
        if index < 0 or index >= len(pages):
            raise ToolError(_MSG_NO_TAB_AT_INDEX.format(index=index, count=len(pages)))
        await pages[index].close()
        # Clamp the active index to the now-shorter tab list.
        remaining = len(self._pages)
        if self._active >= remaining:
            self._active = max(0, remaining - 1)
        self._invalidate_refs()
        return f"[closed tab {index}]"

    # --- state capture / restore (for session resume) ----------------------

    async def capture_state(
        self,
    ) -> Optional[Tuple[List[str], int, Optional[Dict[str, Any]]]]:
        """Capture ``(urls, active, storage_state)`` for resume.

        ``urls`` are the open tabs' URLs in tab order, ``active`` the active-tab
        index, ``storage_state`` the context's ``{cookies, origins}`` (the
        logged-in session). Best-effort: returns ``None`` on any failure (e.g.
        the browser is already gone).
        """
        # Best-effort: a torn-down browser has no context to capture from.
        if self._context is None:
            return None
        try:
            pages = self._pages
            urls = [p.url for p in pages]
            # Drop blank tabs (about:blank / empty) so resume re-opens only real
            # navigations. Keep the list aligned with the active index.
            try:
                storage_state = await self._context.storage_state()
            except Exception as exc:  # noqa: BLE001 — storage capture is best-effort
                logger.debug(f"Browser: storage_state capture failed: {exc}")
                storage_state = None
            return (urls, self._active, cast("Optional[Dict[str, Any]]", storage_state))
        except Exception as exc:  # noqa: BLE001 — capture must not break the call
            logger.debug(f"Browser: state capture failed: {exc}")
            return None

    async def restore_state(
        self,
        urls: List[str],
        active: int = 0,
        storage_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Re-open the saved tabs in this (fresh) browser, seeded with the session.

        ``storage_state`` is applied at :meth:`start` (context creation), so this
        only re-navigates the tabs. Best-effort: never raises (a tab that fails
        to load just stays where it landed). Skips blank/empty URLs.
        """
        try:
            real = [u for u in urls if u and u != "about:blank"]
            if not real:
                return
            assert self._context is not None, "restore_state requires a started browser"
            pages = self._pages
            for i, url in enumerate(real):
                # Reuse the initial blank tab for the first URL, open tabs after.
                if i == 0 and pages:
                    page = pages[0]
                else:
                    page = await self._context.new_page()
                try:
                    await page.goto(url, timeout=DEFAULT_NAV_TIMEOUT_MS)
                except Exception as exc:  # noqa: BLE001 — one bad tab must not abort
                    logger.debug(f"Browser: restore goto {url!r} failed: {exc}")
            n = len(self._pages)
            self._active = active if 0 <= active < n else max(0, n - 1)
        except Exception as exc:  # noqa: BLE001 — restore is best-effort
            logger.debug(f"Browser: tab restore failed: {exc}")

    # --- teardown ----------------------------------------------------------

    async def shutdown(self) -> None:
        """Graceful async teardown (close context + browser + driver).

        When ATTACHED over CDP we own neither the context nor the browser (they
        are the human's real Chrome), so we skip closing them and only stop our
        local Playwright driver — disconnecting cleanly without killing their
        browser or tabs.
        """
        self._closed = True
        if not self._attached:
            for closer in (
                getattr(self._context, "close", None),
                getattr(self._browser, "close", None),
            ):
                if closer is not None:
                    try:
                        await closer()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"Browser: close during shutdown failed: {exc}")
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Browser: context __aexit__ failed, killing: {exc}")
                self.kill()
        self._context = None
        self._browser = None
        self._pw = None
        self._cm = None

    def kill(self) -> None:
        """Best-effort synchronous teardown (idempotent) — for cleanup_session.

        Playwright's teardown is async (``close`` / ``__aexit__`` are coroutines)
        but cleanup runs synchronously, so — mirroring the kernel's ``kill`` —
        we SIGKILL the Playwright driver's node subprocess directly. That tears
        down the launched Chromium with it. The driver is an
        ``asyncio.subprocess.Process`` (no ``poll()``); we use ``os.kill`` on its
        pid so no event loop is needed.
        """
        self._closed = True
        proc = getattr(
            getattr(getattr(self._cm, "_connection", None), "_transport", None),
            "_proc",
            None,
        )
        pid = getattr(proc, "pid", None)
        if pid is not None and getattr(proc, "returncode", None) is None:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        self._context = None
        self._browser = None
        self._pw = None
        self._cm = None
