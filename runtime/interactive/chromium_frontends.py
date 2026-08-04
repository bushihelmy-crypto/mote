"""Media-specific frontends hosted by the shared Chromium live-window shell."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files

from mote.contracts.surface import NOTEBOOK_MEDIA_TYPE, TERMINAL_MEDIA_TYPE

_SHELL_STYLE = r"""
    :root { color-scheme: dark; font: 13px system-ui, sans-serif; }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #15171a; color: #eef1f5; }
    body { display: grid; grid-template-rows: 42px 1fr; }
    #bar { display: flex; align-items: center; gap: 10px; padding: 6px 10px; background: #22252a; }
    #actions { display: flex; gap: 6px; }
    #surface-title { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #authority { padding: 4px 8px; border-radius: 999px; background: #39404a; }
    body.editable #authority { background: #176b45; }
    #frontend-root { min-height: 0; min-width: 0; }
"""

_SHELL_SCRIPT = r"""
    (() => {
      const authority = document.querySelector('#authority');
      const title = document.querySelector('#surface-title');
      window.__moteEmit = (kind, data = '') => {
        if (!document.body.classList.contains('editable') || typeof window.__moteInput !== 'function') return;
        void window.__moteInput({kind, data});
      };
      window.__moteSetEditable = (editable) => {
        const active = Boolean(editable);
        document.body.classList.toggle('editable', active);
        authority.textContent = active ? 'Human control' : 'Observer';
        if (typeof window.__moteSetFrontendEditable === 'function') window.__moteSetFrontendEditable(active);
      };
      window.__moteRender = (frame) => {
        document.body.dataset.surfaceKind = frame.kind || '';
        document.title = frame.title || frame.kind || 'Mote Live Surface';
        title.textContent = document.title;
        if (typeof window.__moteRenderFrame === 'function') window.__moteRenderFrame(frame);
      };
    })();
"""

_SCREENSHOT_STYLE = r"""
    #frontend-root { height: 100%; }
    #stage { width: 100%; height: 100%; position: relative; display: grid; place-items: center; outline: none; }
    #screen { max-width: 100%; max-height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none; }
    #empty { color: #9aa0aa; }
    #screen[src] + #empty { display: none; }
    #back { width: 32px; height: 28px; border: 0; border-radius: 6px; background: #343840; color: #fff; }
"""

_SCREENSHOT_BODY = r"""
    <div id="stage" tabindex="0">
      <img id="screen" alt="Live Runtime surface">
      <div id="empty">Waiting for the first frame…</div>
    </div>
"""

_SCREENSHOT_SCRIPT = r"""
    (() => {
      const state = {kind: '', pointer: null};
      const stage = document.querySelector('#stage');
      const screen = document.querySelector('#screen');
      const back = document.createElement('button');
      back.id = 'back';
      back.title = 'Back';
      back.textContent = '←';
      document.querySelector('#actions').append(back);
      window.__moteSetFrontendEditable = (editable) => { if (editable) stage.focus(); };
      window.__moteRenderFrame = (frame) => {
        state.kind = frame.kind;
        let payload = {};
        try { payload = JSON.parse(frame.content || '{}'); } catch (_) {}
        document.querySelector('#surface-title').textContent = payload.tabs || payload.outline || frame.title || frame.kind;
        const image = payload.screenshot_b64 || '';
        if (image) screen.src = `data:image/png;base64,${image}`;
      };
      const surfacePoint = (event) => {
        const rect = screen.getBoundingClientRect();
        return {
          x: (event.clientX - rect.left) * screen.naturalWidth / rect.width,
          y: (event.clientY - rect.top) * screen.naturalHeight / rect.height,
        };
      };
      screen.addEventListener('pointerdown', (event) => {
        if (!document.body.classList.contains('editable') || event.button !== 0 || !screen.naturalWidth) return;
        event.preventDefault();
        stage.focus();
        screen.setPointerCapture(event.pointerId);
        state.pointer = {...surfacePoint(event), at: performance.now(), id: event.pointerId};
      });
      screen.addEventListener('pointerup', (event) => {
        const start = state.pointer;
        state.pointer = null;
        if (!document.body.classList.contains('editable') || !start || start.id !== event.pointerId) return;
        event.preventDefault();
        const end = surfacePoint(event);
        if (state.kind !== 'device') {
          const distance = Math.hypot(end.x - start.x, end.y - start.y);
          if (distance >= 12) {
            window.__moteEmit('browser.drag', JSON.stringify({x: start.x, y: start.y, x2: end.x, y2: end.y}));
          } else {
            window.__moteEmit('browser.pointer', JSON.stringify(end));
          }
          return;
        }
        const distance = Math.hypot(end.x - start.x, end.y - start.y);
        const duration = performance.now() - start.at;
        if (distance >= 12) {
          window.__moteEmit('device.swipe', JSON.stringify({x: start.x, y: start.y, x2: end.x, y2: end.y}));
        } else if (duration >= 600) {
          window.__moteEmit('device.long_press', JSON.stringify({x: start.x, y: start.y}));
        } else {
          window.__moteEmit('device.tap', JSON.stringify(end));
        }
      });
      screen.addEventListener('pointercancel', () => { state.pointer = null; });
      stage.addEventListener('keydown', (event) => {
        if (!document.body.classList.contains('editable')) return;
        event.preventDefault();
        const prefix = state.kind === 'device' ? 'device.' : 'browser.';
        if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          window.__moteEmit(`${prefix}text`, event.key);
          return;
        }
        const modifiers = [];
        if (event.ctrlKey) modifiers.push('Control');
        if (event.altKey) modifiers.push('Alt');
        if (event.metaKey) modifiers.push('Meta');
        if (event.shiftKey && event.key.length > 1) modifiers.push('Shift');
        const key = event.key === ' ' ? 'Space' : event.key;
        window.__moteEmit(`${prefix}key`, [...modifiers, key].join('+'));
      });
      stage.addEventListener('paste', (event) => {
        if (!document.body.classList.contains('editable')) return;
        event.preventDefault();
        const text = event.clipboardData?.getData('text/plain') || '';
        window.__moteEmit(state.kind === 'device' ? 'device.text' : 'browser.text', text);
      });
      back.addEventListener('click', () => {
        window.__moteEmit(state.kind === 'device' ? 'device.key' : 'browser.back', state.kind === 'device' ? 'BACK' : '');
      });
    })();
"""

_NOTEBOOK_STYLE = r"""
    #frontend-root { height: 100%; display: grid; grid-template-rows: 1fr auto; background: #101216; }
    #notebook { min-height: 0; overflow: auto; padding: 18px max(18px, calc((100vw - 1100px) / 2)); }
    .cell { margin: 0 0 14px; border: 1px solid #30343b; border-radius: 8px; overflow: hidden; background: #191c21; }
    .cell-meta { padding: 5px 10px; color: #9ca5b3; background: #22262d; font-size: 12px; }
    .cell-source, .output-text { margin: 0; padding: 12px; white-space: pre-wrap; overflow-wrap: anywhere; font: 13px ui-monospace, monospace; }
    .outputs { border-top: 1px solid #30343b; background: #121419; }
    .output-text.error { color: #ff8b8b; }
    .output-image { display: block; max-width: 100%; padding: 12px; }
    #composer { display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 12px; border-top: 1px solid #30343b; background: #1a1d22; }
    #source { min-height: 84px; max-height: 35vh; resize: vertical; padding: 10px; border: 1px solid #404651; border-radius: 7px; background: #101216; color: #eef1f5; font: 13px ui-monospace, monospace; }
    #run { align-self: end; padding: 8px 16px; border: 0; border-radius: 7px; background: #176b45; color: white; }
    #run:disabled, #source:disabled { opacity: .55; }
    #stdin { display: none; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; padding: 12px; border-top: 1px solid #5f4d27; background: #272115; }
    #stdin.active { display: grid; }
    #stdin-prompt { color: #f0cf82; white-space: pre-wrap; }
    #stdin-value { min-width: 0; padding: 8px; border: 1px solid #706035; border-radius: 6px; background: #101216; color: #eef1f5; font: 13px ui-monospace, monospace; }
    #stdin-send { padding: 8px 14px; border: 0; border-radius: 6px; background: #8a6822; color: white; }
    .notice { margin-bottom: 12px; color: #e1b85b; }
"""

_NOTEBOOK_BODY = r"""
    <main id="notebook"></main>
    <form id="stdin">
      <label id="stdin-prompt" for="stdin-value"></label>
      <input id="stdin-value" autocomplete="off">
      <button id="stdin-send" type="submit">Send</button>
    </form>
    <div id="composer">
      <textarea id="source" spellcheck="false" placeholder="Enter Python code"></textarea>
      <button id="run" type="button">Run</button>
    </div>
"""

_NOTEBOOK_SCRIPT = r"""
    (() => {
      const notebook = document.querySelector('#notebook');
      const source = document.querySelector('#source');
      const run = document.querySelector('#run');
      const stdin = document.querySelector('#stdin');
      const stdinPrompt = document.querySelector('#stdin-prompt');
      const stdinValue = document.querySelector('#stdin-value');
      let inputRequest = null;
      let editable = false;
      const appendText = (parent, text, error = false) => {
        const pre = document.createElement('pre');
        pre.className = `output-text${error ? ' error' : ''}`;
        pre.textContent = text || '';
        parent.append(pre);
      };
      window.__moteSetFrontendEditable = (editable) => {
        window.__moteNotebookEditable = Boolean(editable);
        source.disabled = !editable;
        run.disabled = !editable;
        stdinValue.disabled = !editable;
        document.querySelector('#stdin-send').disabled = !editable;
        if (editable && inputRequest) stdinValue.focus();
        else if (editable) source.focus();
      };
      window.__moteRenderFrame = (frame) => {
        let documentState = {cells: [], kernel_status: 'idle'};
        try { documentState = JSON.parse(frame.content || '{}'); } catch (_) {}
        editable = Boolean(window.__moteNotebookEditable);
        inputRequest = documentState.input_request || null;
        stdin.classList.toggle('active', Boolean(inputRequest));
        stdinPrompt.textContent = inputRequest?.prompt || 'Input requested';
        stdinValue.type = inputRequest?.password ? 'password' : 'text';
        stdinValue.disabled = !editable;
        document.querySelector('#surface-title').textContent = `${frame.title || 'Jupyter Notebook'} · ${documentState.kernel_status || 'unknown'}`;
        notebook.replaceChildren();
        if (documentState.truncated) {
          const notice = document.createElement('div');
          notice.className = 'notice';
          notice.textContent = 'Earlier cells were removed from this live view.';
          notebook.append(notice);
        }
        for (const cell of documentState.cells || []) {
          const article = document.createElement('article');
          article.className = 'cell';
          const meta = document.createElement('div');
          meta.className = 'cell-meta';
          meta.textContent = `${cell.origin || 'agent'} · ${cell.status || 'complete'} · In [${cell.execution_count ?? ' '}]:`;
          const code = document.createElement('pre');
          code.className = 'cell-source';
          code.textContent = cell.source || '';
          article.append(meta, code);
          if ((cell.outputs || []).length) {
            const outputs = document.createElement('div');
            outputs.className = 'outputs';
            for (const output of cell.outputs) {
              if (output.output_type === 'stream') appendText(outputs, output.text || '');
              if (output.output_type === 'error') appendText(outputs, (output.traceback || []).join('\n') || `${output.ename || ''}: ${output.evalue || ''}`, true);
              const plain = output.data?.['text/plain'];
              if (plain) appendText(outputs, plain);
              const png = output.data?.['image/png'];
              if (png) {
                const image = document.createElement('img');
                image.className = 'output-image';
                image.alt = 'Python display output';
                image.src = `data:image/png;base64,${png}`;
                outputs.append(image);
              }
            }
            article.append(outputs);
          }
          notebook.append(article);
        }
        notebook.scrollTop = notebook.scrollHeight;
      };
      const submit = () => {
        const code = source.value.trimEnd();
        if (!code) return;
        const entropy = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        window.__moteEmit('notebook.execute', JSON.stringify({cell_id: `cell-${entropy}`, source: code}));
        source.value = '';
      };
      run.addEventListener('click', submit);
      stdin.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!inputRequest || !editable) return;
        window.__moteEmit('notebook.input_reply', JSON.stringify({
          request_id: inputRequest.request_id,
          value: stdinValue.value,
          document_revision: inputRequest.document_revision,
          kernel_epoch: inputRequest.kernel_epoch,
          connection_generation: inputRequest.connection_generation,
          human_generation: inputRequest.human_generation,
          expected_request_revision: inputRequest.request_revision
        }));
        stdinValue.value = '';
      });
      source.addEventListener('keydown', (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
          event.preventDefault();
          submit();
        }
      });
    })();
"""

_TERMINAL_STYLE = r"""
    #frontend-root { height: 100%; padding: 8px; background: #101216; }
    #terminal { width: 100%; height: 100%; }
"""

_TERMINAL_BODY = r"""<div id="terminal"></div>"""

_TERMINAL_SCRIPT = r"""
    (() => {
      const terminal = new Terminal({
        allowProposedApi: false,
        cursorBlink: true,
        convertEol: false,
        disableStdin: true,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 13,
        scrollback: 10000,
        theme: {background: '#101216', foreground: '#eef1f5'}
      });
      const fit = new FitAddon.FitAddon();
      terminal.loadAddon(fit);
      terminal.open(document.querySelector('#terminal'));
      let lastSequence = -1;
      const resize = () => {
        try { fit.fit(); } catch (_) {}
      };
      terminal.onData((data) => window.__moteEmit('terminal.input', data));
      terminal.onResize(({cols, rows}) => {
        window.__moteEmit('terminal.resize', JSON.stringify({cols, rows}));
      });
      window.__moteSetFrontendEditable = (editable) => {
        terminal.options.disableStdin = !editable;
        resize();
        if (editable) terminal.focus();
      };
      window.__moteRenderFrame = (frame) => {
        const content = frame.content || '';
        const metadata = frame.metadata || {};
        const baseSequence = Number(metadata.base_sequence ?? -1);
        const incremental = metadata.mode === 'delta' && baseSequence === lastSequence;
        if (!incremental) {
          terminal.reset();
        }
        if (content) terminal.write(content);
        lastSequence = Number(frame.sequence ?? lastSequence);
      };
      new ResizeObserver(resize).observe(document.querySelector('#terminal'));
      resize();
    })();
"""


@dataclass(frozen=True, slots=True)
class ChromiumSurfaceFrontend:
    """One media renderer mounted inside the shared live-window shell."""

    name: str
    media_types: frozenset[str]
    style: str
    body: str
    script: str
    asset_styles: tuple[str, ...] = ()
    asset_scripts: tuple[str, ...] = ()

    def document(self) -> str:
        styles = "".join(self._read_asset(path) for path in self.asset_styles)
        return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <meta name=\"color-scheme\" content=\"dark\">
  <title>Mote Live Surface</title>
  <style>{_SHELL_STYLE}{styles}{self.style}</style>
</head>
<body>
  <header id=\"bar\"><div id=\"actions\"></div><div id=\"surface-title\">Mote Live Surface</div><div id=\"authority\">Observer</div></header>
  <div id=\"frontend-root\">{self.body}</div>
</body>
</html>"""

    def scripts(self) -> tuple[str, ...]:
        return tuple(self._read_asset(path) for path in self.asset_scripts) + (
            self.script,
            _SHELL_SCRIPT,
        )

    @staticmethod
    def _read_asset(path: str) -> str:
        return files("mote.runtime.interactive").joinpath(path).read_text(encoding="utf-8")


SCREENSHOT_FRONTEND = ChromiumSurfaceFrontend(
    name="screenshot",
    media_types=frozenset(
        {
            "application/vnd.mote.browser+json",
            "application/vnd.mote.device+json",
        }
    ),
    style=_SCREENSHOT_STYLE,
    body=_SCREENSHOT_BODY,
    script=_SCREENSHOT_SCRIPT,
)

NOTEBOOK_FRONTEND = ChromiumSurfaceFrontend(
    name="notebook",
    media_types=frozenset({NOTEBOOK_MEDIA_TYPE}),
    style=_NOTEBOOK_STYLE,
    body=_NOTEBOOK_BODY,
    script=_NOTEBOOK_SCRIPT,
)

TERMINAL_FRONTEND = ChromiumSurfaceFrontend(
    name="terminal",
    media_types=frozenset({TERMINAL_MEDIA_TYPE}),
    style=_TERMINAL_STYLE,
    body=_TERMINAL_BODY,
    script=_TERMINAL_SCRIPT,
    asset_styles=("assets/xterm/xterm.css",),
    asset_scripts=("assets/xterm/xterm.js", "assets/xterm/addon-fit.js"),
)

DEFAULT_CHROMIUM_FRONTENDS = (
    SCREENSHOT_FRONTEND,
    NOTEBOOK_FRONTEND,
    TERMINAL_FRONTEND,
)


__all__ = [
    "ChromiumSurfaceFrontend",
    "DEFAULT_CHROMIUM_FRONTENDS",
    "NOTEBOOK_FRONTEND",
    "SCREENSHOT_FRONTEND",
    "TERMINAL_FRONTEND",
]
