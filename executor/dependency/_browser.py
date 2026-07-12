"""Persistent web-browser engine — the backend for the single ``WebBrowser`` tool.

The browser sibling of the persistent ``terminal`` / ``python`` engines: instead
of a PTY-backed shell or a Jupyter kernel this keeps **one live Playwright
(Chromium) browser per Role session** that the model drives across calls:

  * open tabs, the navigated URLs, and the logged-in session (cookies /
    localStorage) persist between calls, so browsing state is built up step by
    step;
  * each action (navigate / click / type / read / screenshot / eval / back /
    tab management) runs against the live page and returns its result;
  * ``close`` shuts the browser down.

The live :class:`BrowserSession` is owned by the Role: the ``WebBrowser`` tool
stores it on the Role's ``RoleState`` (one implicit browser per session, like the
``terminal`` / ``python`` tools — there is no model-facing browser id) rather than
in a process-global registry, so browsers are isolated per Role and torn down
with it. This module owns only the engine; the per-Role lifecycle lives in the
tool.

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
from typing import Any, Dict, List, Optional, Tuple

from metagpt.common.logs import logger
from metagpt.executor.tool_result import ToolError

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


def _cap_text(text: str) -> str:
    """Cap *text* at :data:`TEXT_MAX_CHARS`, keeping head + tail, drop middle."""
    if len(text) <= TEXT_MAX_CHARS:
        return text
    head = TEXT_MAX_CHARS // 2
    tail = TEXT_MAX_CHARS - head
    omitted = len(text) - TEXT_MAX_CHARS
    return f"{text[:head]}\n[... {omitted} chars omitted ...]\n{text[-tail:]}"


# --- Injected JS ------------------------------------------------------------
# A single page.evaluate that walks the DOM, decides which elements are
# interactive (browser-use-style multi-layer heuristics), stamps each with a
# ``data-agent-ref`` index attribute, and returns metadata the engine
# serializes into the ``[N]<tag>`` snapshot the model reads. Returns
# ``{"elements": [...]}`` where each element is
# ``{ref, tag, role, name, type, value, inViewport}``.
#
# Interactivity signals (any one qualifies): interactive tag, ARIA role,
# on*/tabindex attributes, contenteditable, or ``cursor: pointer``.
# Visibility: must have layout boxes (offsetParent or non-zero rect) and not
# be display:none / visibility:hidden / opacity:0.
_SNAPSHOT_JS = r"""
() => {
  const INTERACTIVE_TAGS = new Set([
    'a','button','input','select','textarea','details','summary','option','label'
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button','link','menuitem','option','radio','checkbox','tab','textbox',
    'combobox','slider','spinbutton','search','searchbox','switch','menuitemcheckbox',
    'menuitemradio','treeitem'
  ]);
  const EVENT_ATTRS = ['onclick','onmousedown','onmouseup','onkeydown','onkeyup'];

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity || '1') === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) {
      // bare <a> without href / <label> are weak; still include if they have text
      if (tag === 'a' && !el.getAttribute('href') && !el.onclick) {
        // allow if it has a click-ish role/tabindex below
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

  // Clear any stale refs from a previous snapshot on this page.
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));

  const out = [];
  let idx = 0;
  const all = document.querySelectorAll('*');
  for (const el of all) {
    if (!isVisible(el)) continue;
    if (!isInteractive(el)) continue;
    idx += 1;
    const ref = String(idx);
    el.setAttribute('data-agent-ref', ref);
    const rect = el.getBoundingClientRect();
    const inViewport = rect.bottom > 0 && rect.top < (window.innerHeight || 0)
      && rect.right > 0 && rect.left < (window.innerWidth || 0);
    out.push({
      ref: ref,
      tag: el.tagName.toLowerCase(),
      role: (el.getAttribute('role') || '').toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: accessibleName(el),
      value: el.value !== undefined ? String(el.value || '') : '',
      placeholder: el.getAttribute('placeholder') || '',
      href: el.getAttribute('href') || '',
      checked: (el.checked === true),
      inViewport: inViewport,
    });
  }
  return { elements: out, viewportHeight: window.innerHeight || 0 };
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
  let hit = document.elementFromPoint(x, y);
  if (!hit) return null;  // off-screen; click() will scroll it in
  if (hit === el) return null;
  for (let n = hit; n; n = n.parentElement) { if (n === el) return null; }
  for (let n = el; n; n = n.parentElement) { if (n === hit) return null; }
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


# --- Markdown extraction ----------------------------------------------------
# Walk the rendered DOM and emit a clean, token-dense Markdown rendering of the
# main content: headings, paragraphs, lists, links, code, and table rows.
# Noise containers (script/style/nav/footer/aside/svg/header/form controls) and
# hidden nodes are skipped. Returns a single Markdown string. Mirrors obscura's
# markdown walker (kept deliberately small; not a full readability port).
_MARKDOWN_JS = r"""
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
  function walk(node) {
    if (node.nodeType === 3) {  // text
      return node.nodeValue.replace(/\s+/g, ' ');
    }
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return '';
    if (isHidden(node)) return '';
    let inner = '';
    for (const child of node.childNodes) inner += walk(child);
    const trimmed = inner.trim();
    switch (tag) {
      case 'h1': return trimmed ? '\n\n# ' + trimmed + '\n\n' : '';
      case 'h2': return trimmed ? '\n\n## ' + trimmed + '\n\n' : '';
      case 'h3': return trimmed ? '\n\n### ' + trimmed + '\n\n' : '';
      case 'h4': return trimmed ? '\n\n#### ' + trimmed + '\n\n' : '';
      case 'h5': return trimmed ? '\n\n##### ' + trimmed + '\n\n' : '';
      case 'h6': return trimmed ? '\n\n###### ' + trimmed + '\n\n' : '';
      case 'p': return trimmed ? '\n\n' + trimmed + '\n\n' : '';
      case 'br': return '\n';
      case 'hr': return '\n\n---\n\n';
      case 'li': return trimmed ? '\n- ' + trimmed : '';
      case 'ul': case 'ol': return '\n' + inner + '\n';
      case 'blockquote': return trimmed ? '\n\n> ' + trimmed + '\n\n' : '';
      case 'pre': return trimmed ? '\n\n```\n' + inner.replace(/^\n+|\n+$/g, '') + '\n```\n\n' : '';
      case 'code': return trimmed ? '`' + trimmed + '`' : '';
      case 'strong': case 'b': return trimmed ? '**' + trimmed + '**' : '';
      case 'em': case 'i': return trimmed ? '*' + trimmed + '*' : '';
      case 'a': {
        const href = node.getAttribute('href');
        if (trimmed && href) return '[' + trimmed + '](' + href + ')';
        return trimmed;
      }
      case 'img': {
        const alt = node.getAttribute('alt') || '';
        const src = node.getAttribute('src') || '';
        return alt || src ? '![' + alt + '](' + src + ')' : '';
      }
      case 'tr': return '\n| ' + inner.trim() + ' |';
      case 'td': case 'th': return trimmed + ' | ';
      case 'table': return '\n\n' + inner + '\n\n';
      default: return inner;
    }
  }
  const root = document.body || document.documentElement;
  let md = walk(root);
  // collapse 3+ blank lines into a single blank line
  md = md.replace(/\n{3,}/g, '\n\n').replace(/[ \t]+\n/g, '\n');
  return md.trim();
}
"""


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
    """Render one snapshot element as ``[N]<tag attrs>name``.

    Mirrors browser-use's serialization: the ``[N]`` index is what the model
    passes back to ``click``/``type``; a small whitelist of attributes
    (type/placeholder/checked/href) is shown, ``class`` and other noise omitted
    to keep the listing token-efficient.
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
    ) -> None:
        self.session_key = session_key
        self.cwd = cwd
        self.headless = headless
        self._cm = None  # async_playwright() context manager
        self._pw = None  # the started Playwright object
        self._browser = None  # launched Browser
        self._context = None  # BrowserContext (shared cookies/storage)
        self._closed = False
        # Index of the active tab within ``self._context.pages``.
        self._active = 0
        # Last snapshot's ref metadata: {ref(str): {tag, role, name, ...}}.
        # Populated by snapshot(); used to give actionable errors when a ref is
        # acted on. Stamped onto the DOM as data-agent-ref, so refs survive
        # until the next snapshot or navigation.
        self._ref_meta: Dict[str, Dict[str, Any]] = {}

    # --- lifecycle ---------------------------------------------------------

    async def start(self, *, storage_state: Optional[Dict[str, Any]] = None) -> None:
        """Launch the browser + a fresh context, optionally seeded with a session.

        ``storage_state`` (a Playwright ``{cookies, origins}`` dict) re-seeds the
        logged-in session on resume so re-opened tabs are already authenticated.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:  # pragma: no cover - depends on optional dep
            raise ToolError(
                "Error: the WebBrowser tool requires Playwright. Install it with "
                "`pip install playwright` and `playwright install chromium`."
            ) from e
        try:
            self._cm = async_playwright()
            self._pw = await asyncio.wait_for(self._cm.start(), timeout=_LAUNCH_TIMEOUT_S)
            self._browser = await self._pw.chromium.launch(headless=self.headless)
            ctx_kwargs: dict = {}
            if storage_state:
                ctx_kwargs["storage_state"] = storage_state
            self._context = await self._browser.new_context(**ctx_kwargs)
            # Start with one blank tab so the model always has a page to act on.
            await self._context.new_page()
            self._active = 0
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            self.kill()
            raise ToolError(f"Error: web browser failed to start: {e}")

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
            raise ToolError("Error: the browser has no open tabs.")
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

    async def navigate(self, url: str, *, timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS) -> str:
        """Navigate the active tab to *url*; return a short status line."""
        page = self._active_page()
        await page.goto(url, timeout=timeout_ms)
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
        handle = await self._locate(page, selector, ref, timeout_ms)
        blocker = await self._blocker(page, handle)
        if blocker:
            raise ToolError(
                f"Error: {target} is covered by <{blocker}> — dismiss/handle that "
                f"element first, then take a fresh snapshot and retry."
            )
        await handle.click(timeout=timeout_ms)
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
        handle = await self._locate(page, selector, ref, timeout_ms)
        if clear:
            await handle.fill(text, timeout=timeout_ms)
        else:
            await handle.click(timeout=timeout_ms)
            await handle.type(text, timeout=timeout_ms)
        return f"[typed into {target}]"

    async def _locate(self, page, selector: str, ref: Optional[str], timeout_ms: int):
        """Return a Playwright ElementHandle for *selector*, with ref-aware errors.

        A missing ``[N]`` index almost always means the page changed since the
        last snapshot (the ``data-agent-ref`` attribute was cleared/replaced), so
        we tell the model to re-snapshot rather than emit a raw selector error.
        """
        try:
            handle = await page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
        except Exception as e:  # noqa: BLE001
            if ref is not None:
                known = ref in self._ref_meta
                hint = (
                    "the page changed since the last snapshot" if known else f"no element [{ref}] in the last snapshot"
                )
                raise ToolError(
                    f"Error: element [{ref}] not found ({hint}). Take a fresh "
                    f"snapshot to get current element indices."
                )
            raise ToolError(f"Error: no element matched selector {selector!r}: {e}")
        if handle is None:
            raise ToolError(f"Error: no element matched {selector!r}.")
        return handle

    async def _blocker(self, page, handle) -> Optional[str]:
        """Hit-test *handle*'s center; return the blocking element desc or None."""
        try:
            return await page.evaluate(_BLOCKER_JS, handle)
        except Exception:  # noqa: BLE001 — hit-test is advisory, never fatal
            return None

    async def read(self) -> str:
        """Return the active tab's main content as Markdown (capped).

        Walks the DOM stripping chrome (nav/footer/script/forms/…) and emits a
        Markdown rendering of the readable content — far more agent-friendly than
        a raw innerText dump. Falls back to body innerText (then raw HTML) if the
        Markdown walker fails on an unusual page.
        """
        page = self._active_page()
        header = f"[{page.url}] {await page.title()}\n"
        try:
            md = await page.evaluate(_MARKDOWN_JS)
        except Exception:  # noqa: BLE001 — walker may fail on exotic pages
            md = None
        if md and md.strip():
            return header + _cap_text(md)
        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001 — some pages have no body yet
            text = await page.content()
        return header + _cap_text(text)

    async def snapshot(self) -> str:
        """Stamp interactive elements with ``[N]`` refs and return a text tree.

        Runs :data:`_SNAPSHOT_JS` on the active tab: it walks the DOM, decides
        which elements are interactive, stamps each visible one with a
        ``data-agent-ref`` index, and returns metadata. We serialize that into a
        browser-use-style ``[N]<tag attrs>name`` listing the model reads, then
        drives via ``click``/``type`` with the same ``[N]`` index. The refs are
        stored in ``self._ref_meta`` (for actionable errors) and persist on the
        DOM until the next snapshot or navigation.
        """
        page = self._active_page()
        try:
            data = await page.evaluate(_SNAPSHOT_JS)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Error: failed to snapshot the page: {e}")
        elements = (data or {}).get("elements", []) or []
        self._ref_meta = {el["ref"]: el for el in elements}
        header = f"[{page.url}] {await page.title()}"
        if not elements:
            return f"{header}\n[no interactive elements found]"
        lines = [header]
        offscreen = 0
        for el in elements:
            if not el.get("inViewport", True):
                offscreen += 1
            lines.append(_format_snapshot_line(el))
        if offscreen:
            lines.append(f"[{offscreen} element(s) are off-screen; scroll to bring into view]")
        return _cap_text("\n".join(lines))

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
            raise ToolError("Error: 'wait' needs exactly one of selector or expression.")
        if selector:
            sel, ref = self._resolve_target(selector)
            try:
                await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
            except Exception:  # noqa: BLE001
                raise ToolError(f"Error: timed out after {timeout_ms}ms waiting for {selector!r}.")
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
        raise ToolError(f"Error: timed out after {timeout_ms}ms waiting for {expression!r}{suffix}.")

    async def assist(self, prompt: str, *, ask_human, headless: bool) -> str:
        """Pause automation and ask the human to supply something only they can.

        For things the model must not invent or cannot obtain on its own — the
        user's own private data (phone number, email, account, address), a
        one-time code, a login QR scan, a graphical CAPTCHA. We do **not** bypass
        any check; we ask the real person, then resume from wherever the page
        ended up.

        Two paths, by ``headless``:

        * **Headless** (the safe default — no visible window): we capture a
          screenshot, write it to ``{cwd}/.agent_browser/assist_<ts>.png``, and
          send ``ask_human`` a prompt naming that file plus the page URL. If what
          we need is on the page (a QR / graphical captcha) the user opens the
          image (e.g. ``explorer.exe`` on WSL2) to read it; if it is off the page
          (an emailed / SMS code, the user's own phone or email) they just reply
          with it. Either way we return their reply so the model can act on it
          (e.g. ``type`` the value into a field).
        * **Headed** (a real visible window): we ask the user to act directly in
          the browser window, then resume.

        ``assist`` is single-purpose: it asks and returns the reply — it does
        **not** fill any field itself. The model uses ``type`` / ``fill_form``
        afterward with whatever the user supplies.

        ``ask_human`` (the role's human text channel — text only) and
        ``headless`` are passed in by the tool shell so the engine stays free of
        any Role reference, like the rest of these methods.
        """
        page = self._active_page()
        if not headless:
            question = (
                f"[browser handoff] {prompt}\n"
                f"Current page: {page.url}\n"
                f"Complete this in the browser window, then reply to continue."
            )
            reply = await ask_human(question)  # blocks until the user replies
            return f"[resumed by user] now at {page.url}\nuser said: {reply}"

        # Headless: no window to hand off, so screenshot the page to disk and
        # ask the user. ask_human is text-only, hence the file path. The value
        # may be on the page (open the image to read it) or off it (an emailed /
        # SMS code, the user's own phone or email) — they just reply either way.
        shot_path = await self._save_assist_screenshot()
        question = (
            f"[browser assist] {prompt}\n"
            f"Current page: {page.url}\n"
            f"A screenshot of the page is saved at: {shot_path}\n"
            f"If you need to read something on the page (a QR / captcha), open it "
            f'(e.g. `explorer.exe "{shot_path}"` on WSL2); otherwise just reply '
            f"with the value (e.g. the SMS / email code, your phone or email) to "
            f"continue."
        )
        reply = await ask_human(question)  # blocks until the user replies
        return f"[user replied] now at {page.url}\n" f"screenshot: {shot_path}\n" f"user said: {reply}"

    async def _save_assist_screenshot(self) -> str:
        """Capture the active tab and write it to a session-scoped PNG file.

        Returns the absolute path. The directory is ``{cwd}/.agent_browser`` (cwd
        falls back to the process cwd when the session has none).
        """
        png = await self.screenshot()
        base = self.cwd or os.getcwd()
        shot_dir = os.path.join(base, ".agent_browser")
        os.makedirs(shot_dir, exist_ok=True)
        fname = f"assist_{int(time.time() * 1000)}.png"
        path = os.path.abspath(os.path.join(shot_dir, fname))
        with open(path, "wb") as f:
            f.write(png)
        return path

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
            raise ToolError(f"Error: failed to detect forms: {e}")
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
            raise ToolError("Error: 'fill_form' needs a non-empty {selector: value} mapping.")
        page = self._active_page()
        filled = []
        for target, value in fields.items():
            selector, ref = self._resolve_target(str(target))
            handle = await self._locate(page, selector, ref, timeout_ms)
            try:
                await handle.fill(str(value), timeout=timeout_ms)
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"Error: failed to fill {target!r}: {e}")
            filled.append(str(target))
        msg = f"[filled {len(filled)} field(s): {', '.join(filled)}]"
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
            raise ToolError("Error: 'extract' needs a non-empty {key: 'selector[@attr]'} mapping.")
        page = self._active_page()
        try:
            data = await page.evaluate(_EXTRACT_JS, schema)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"Error: failed to extract: {e}")
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def screenshot(self) -> bytes:
        """Capture the active tab as a PNG and return the raw bytes."""
        page = self._active_page()
        return await page.screenshot(type="png", full_page=False)

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
        page = await self._context.new_page()
        self._active = len(self._pages) - 1
        if url:
            await page.goto(url, timeout=DEFAULT_NAV_TIMEOUT_MS)
            return f"[opened tab {self._active}: {page.url}]"
        return f"[opened blank tab {self._active}]"

    def switch_tab(self, index: int) -> str:
        """Make the tab at *index* the active one."""
        pages = self._pages
        if index < 0 or index >= len(pages):
            raise ToolError(f"Error: no tab at index {index} (have {len(pages)}).")
        self._active = index
        return f"[switched to tab {index}: {pages[index].url}]"

    async def close_tab(self, index: int) -> str:
        """Close the tab at *index*, clamping the active index afterwards."""
        pages = self._pages
        if index < 0 or index >= len(pages):
            raise ToolError(f"Error: no tab at index {index} (have {len(pages)}).")
        await pages[index].close()
        # Clamp the active index to the now-shorter tab list.
        remaining = len(self._pages)
        if self._active >= remaining:
            self._active = max(0, remaining - 1)
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
            return (urls, self._active, storage_state)
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
        """Graceful async teardown (close context + browser + driver)."""
        self._closed = True
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
