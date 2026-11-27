#!/usr/bin/env python3
"""Stdlib-only MCP server implementing chrome-devtools-mcp over Android WebView CDP.

Connects to Droidgravity's localhost CdpProxyServer discovered via DevToolsActivePort
($HOME/.config/{google-chrome,chromium}/DevToolsActivePort) and exposes the standard
chrome_devtools MCP tool suite expected by Antigravity browser agents.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

SPECIAL_KEYS: Dict[str, Tuple[str, str, int]] = {
    "enter": ("Enter", "Enter", 13),
    "return": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "space": (" ", "Space", 32),
    " ": (" ", "Space", 32),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "up": ("ArrowUp", "ArrowUp", 38),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "down": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "left": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "right": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
}


SNAPSHOT_JS = r"""
(() => {
  const MAX_NODES = 1200;
  const refs = {};
  let seq = 0;
  const lines = [];

  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) {
      return false;
    }
    if (el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true') {
      return false;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 && rect.height <= 0) {
      // Container with display:contents may still wrap visible children
      if (style.display !== 'contents') return false;
    }
    return true;
  }

  function cleanText(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  function getDirectText(el) {
    let out = '';
    for (const child of el.childNodes) {
      if (child.nodeType === 3) {
        out += child.nodeValue || '';
      }
    }
    return cleanText(out);
  }

  function getImplicitRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.trim().split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'button' || t === 'submit' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'search') return 'searchbox';
      if (t === 'hidden') return '';
      return 'textbox';
    }
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'img') return 'img';
    if (tag === 'nav') return 'navigation';
    if (tag === 'main') return 'main';
    if (tag === 'header') return 'banner';
    if (tag === 'footer') return 'contentinfo';
    if (tag === 'form') return 'form';
    if (tag === 'li') return 'listitem';
    if (tag === 'ul' || tag === 'ol') return 'list';
    if (tag === 'table') return 'table';
    if (tag === 'tr') return 'row';
    if (tag === 'td') return 'cell';
    if (tag === 'th') return 'columnheader';
    if (tag === 'summary') return 'button';
    if (tag === 'p' || tag === 'blockquote' || tag === 'pre') return 'paragraph';
    if (el.isContentEditable) return 'textbox';
    if (el.hasAttribute('onclick') || el.getAttribute('tabindex') === '0') return 'generic';
    return '';
  }

  function isInteractive(el, role) {
    if (['link', 'button', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox', 'listbox', 'slider', 'menuitem', 'tab', 'switch', 'option'].includes(role)) {
      return true;
    }
    const tag = el.tagName.toLowerCase();
    if (['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag)) return true;
    if (el.isContentEditable || el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex');
    return ti !== null && ti !== '-1';
  }

  function getAccessibleName(el, role) {
    const aria = cleanText(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map(id => {
        const ref = document.getElementById(id);
        return ref ? cleanText(ref.innerText || ref.textContent) : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const tag = el.tagName.toLowerCase();
    if ((tag === 'input' || tag === 'textarea' || tag === 'select') && el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) {
        const lt = cleanText(lbl.innerText || lbl.textContent);
        if (lt) return lt;
      }
    }
    const closestLabel = (tag === 'input' || tag === 'textarea' || tag === 'select') ? el.closest('label') : null;
    if (closestLabel) {
      const lt = cleanText(closestLabel.innerText || closestLabel.textContent);
      if (lt) return lt;
    }
    if (tag === 'img') {
      return cleanText(el.getAttribute('alt') || el.getAttribute('title') || '');
    }
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'button' || t === 'submit' || t === 'reset') {
        return cleanText(el.value || el.getAttribute('title') || '');
      }
      return cleanText(el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '');
    }
    if (tag === 'textarea') {
      return cleanText(el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title') || '');
    }
    if (isInteractive(el, role) || ['heading', 'listitem', 'cell', 'columnheader'].includes(role)) {
      const text = cleanText(el.innerText || el.textContent);
      if (text) return text.slice(0, 180);
    }
    return cleanText(el.getAttribute('title') || '');
  }

  function pushLine(depth, uid, role, name, extras) {
    if (lines.length >= MAX_NODES) return;
    const indent = '  '.repeat(Math.min(depth, 10));
    let line = `${indent}uid=${uid} ${role}`;
    if (name) {
      line += ` ${JSON.stringify(name)}`;
    }
    if (extras && extras.length) {
      line += ' ' + extras.join(' ');
    }
    lines.push(line);
  }

  function nextUid(el) {
    seq += 1;
    const uid = 'e' + seq;
    refs[uid] = el;
    return uid;
  }

  function walk(node, depth) {
    if (!node || lines.length >= MAX_NODES) return;
    if (node.nodeType === 1) {
      const tag = node.tagName.toLowerCase();
      if (tag === 'script' || tag === 'style' || tag === 'noscript' || tag === 'template') return;
      if (!isVisible(node)) return;

      const role = getImplicitRole(node);
      const interactive = isInteractive(node, role);
      const name = getAccessibleName(node, role);
      const directText = getDirectText(node);

      let emitted = false;
      let nextDepth = depth;
      if (interactive || (role && name) || ['navigation', 'main', 'form', 'list', 'table'].includes(role)) {
        const uid = nextUid(node);
        const extras = [];
        if (/^h[1-6]$/.test(tag)) extras.push(`level=${tag.slice(1)}`);
        if (tag === 'a' && node.getAttribute('href')) extras.push(`url=${JSON.stringify(node.getAttribute('href'))}`);
        if (node.disabled) extras.push('disabled');
        if (node.checked === true) extras.push('checked');
        if (document.activeElement === node) extras.push('focused');
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
          const val = node.value;
          if (val !== undefined && val !== '' && !['button', 'submit', 'reset'].includes((node.type || '').toLowerCase())) {
            const masked = (node.type || '').toLowerCase() === 'password' ? '••••' : String(val).slice(0, 120);
            extras.push(`value=${JSON.stringify(masked)}`);
          }
        }
        pushLine(depth, uid, role || tag, name, extras);
        emitted = true;
        nextDepth = depth + 1;
      } else if (directText && tag !== 'title') {
        const uid = nextUid(node);
        pushLine(depth, uid, 'StaticText', directText.slice(0, 200), []);
        emitted = true;
      }

      if (node.shadowRoot) {
        for (const sc of node.shadowRoot.children) {
          walk(sc, nextDepth);
        }
      }
      if (!emitted && directText && tag !== 'title' && node.children.length > 0) {
        const uid = nextUid(node);
        pushLine(depth, uid, 'StaticText', directText.slice(0, 200), []);
      }
      for (const child of node.childNodes) {
        if (child.nodeType === 1) {
          walk(child, nextDepth);
        } else if (child.nodeType === 3 && !emitted && node.children.length > 0) {
          const t = cleanText(child.nodeValue);
          if (t) {
            const uid = nextUid(node);
            pushLine(nextDepth, uid, 'StaticText', t.slice(0, 200), []);
          }
        }
      }
      if (tag === 'iframe') {
        try {
          if (node.contentDocument && node.contentDocument.body) {
            walk(node.contentDocument.body, nextDepth);
          }
        } catch (e) {}
      }
    }
  }

  const rootUid = nextUid(document.documentElement || document.body);
  pushLine(0, rootUid, 'RootWebArea', cleanText(document.title), [`url=${JSON.stringify(location.href)}`]);
  if (document.body) {
    walk(document.body, 1);
  }
  window.__agy_snapshot_refs = refs;
  return lines.join('\n');
})()
"""


class CdpError(RuntimeError):
    pass


class CdpWebSocketClient:
    """Thread-safe RFC 6455 WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, ws_url: str, event_handler: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme != "ws":
            raise ValueError(f"Unsupported CDP WebSocket URL: {ws_url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.ws_url = ws_url
        self.event_handler = event_handler

        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._closed = False
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._next_id = 0
        self._reader_thread: Optional[threading.Thread] = None

    def connect(self, timeout: float = 10.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(timeout)

        key_bytes = os.urandom(16)
        sec_key = base64.b64encode(key_bytes).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(req)

        raw = bytearray()
        while True:
            idx = raw.find(b"\r\n\r\n")
            if idx >= 0:
                header_bytes = bytes(raw[:idx])
                tail = bytes(raw[idx + 4:])
                break
            chunk = sock.recv(4096)
            if not chunk:
                raise CdpError("WebSocket closed during handshake")
            raw.extend(chunk)
            if len(raw) > 65536:
                raise CdpError("WebSocket handshake header too large")

        header_text = header_bytes.decode("ascii", errors="replace")
        lines = header_text.split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise CdpError(f"WebSocket handshake failed: {lines[0] if lines else 'empty'}")

        accept_expected = base64.b64encode(
            hashlib.sha1((sec_key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        accept_actual = None
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip().lower() == "sec-websocket-accept":
                    accept_actual = v.strip()
        if accept_actual != accept_expected:
            raise CdpError("Invalid Sec-WebSocket-Accept from CDP endpoint")

        sock.settimeout(None)
        self._sock = sock
        self._buf.extend(tail)
        self._closed = False
        self._reader_thread = threading.Thread(target=self._read_loop, name="cdp-ws-reader", daemon=True)
        self._reader_thread.start()

    def is_alive(self) -> bool:
        return not self._closed and self._sock is not None

    def close(self) -> None:
        self._closed = True
        if self._sock is not None:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with self._pending_lock:
            for ev, holder in self._pending.values():
                holder["error"] = {"message": "WebSocket connection closed"}
                ev.set()
            self._pending.clear()

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
        if not self.is_alive():
            raise CdpError("CDP WebSocket is not connected")
        ev = threading.Event()
        holder: Dict[str, Any] = {}
        with self._pending_lock:
            self._next_id += 1
            msg_id = self._next_id
            self._pending[msg_id] = (ev, holder)

        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8")
        try:
            self._send_frame(0x1, payload)
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            self.close()
            raise CdpError(f"Failed sending {method}: {exc}") from exc

        if not ev.wait(timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise CdpError(f"CDP command timed out: {method}")

        if "error" in holder:
            err = holder["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise CdpError(f"{method}: {msg}")
        return holder.get("result", {})

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._sock is None:
                raise EOFError("socket closed")
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise EOFError("EOF on WebSocket")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _read_frame(self) -> Tuple[int, bytes]:
        assembled: Optional[bytearray] = None
        first_opcode = -1
        while True:
            hdr = self._recv_exact(2)
            b0, b1 = hdr[0], hdr[1]
            fin = (b0 & 0x80) != 0
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else None
            data = self._recv_exact(length) if length > 0 else b""
            if masked and mask_key is not None:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))

            if opcode & 0x08:
                return opcode, data

            if first_opcode < 0:
                first_opcode = opcode
            if fin and assembled is None:
                return first_opcode, data
            if assembled is None:
                assembled = bytearray()
            assembled.extend(data)
            if fin:
                return first_opcode, bytes(assembled)

    def _send_frame(self, opcode: int, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise CdpError("Socket is closed")
        mask = os.urandom(4)
        length = len(data)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked_data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        packet = bytes(header) + masked_data
        with self._send_lock:
            sock.sendall(packet)

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                opcode, payload = self._read_frame()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    try:
                        self._send_frame(0xA, payload)
                    except Exception:
                        break
                    continue
                if opcode != 0x1:
                    continue

                msg = json.loads(payload.decode("utf-8", errors="replace"))
                if "id" in msg:
                    msg_id = msg["id"]
                    with self._pending_lock:
                        entry = self._pending.pop(msg_id, None)
                    if entry is not None:
                        ev, holder = entry
                        if "error" in msg:
                            holder["error"] = msg["error"]
                        else:
                            holder["result"] = msg.get("result", {})
                        ev.set()
                elif "method" in msg and self.event_handler is not None:
                    try:
                        self.event_handler(msg["method"], msg.get("params") or {})
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.close()


@dataclass
class TargetState:
    page_id: int
    target_id: str
    url: str = "about:blank"
    title: str = ""
    client: Optional[CdpWebSocketClient] = None
    console_messages: deque = field(default_factory=lambda: deque(maxlen=500))
    network_requests: deque = field(default_factory=lambda: deque(maxlen=500))
    network_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pending_dialog: Optional[Dict[str, Any]] = None
    load_event: threading.Event = field(default_factory=threading.Event)
    next_console_id: int = 1
    next_network_id: int = 1


class BrowserController:
    """Manages DevToolsActivePort discovery, page registry, and CDP command execution."""

    def __init__(self, browser_url: Optional[str] = None):
        self.override_browser_url = browser_url
        self._lock = threading.RLock()
        self._next_page_id = 1
        self._id_by_target: Dict[str, int] = {}
        self._states: Dict[str, TargetState] = {}
        self._selected_page_id: Optional[int] = None

    def discover_host_port(self, wait_seconds: float = 3.0) -> Tuple[str, int]:
        if self.override_browser_url:
            parsed = urllib.parse.urlparse(self.override_browser_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return host, port

        deadline = time.monotonic() + wait_seconds
        candidates = self._candidate_port_files()
        while True:
            for path in candidates:
                if os.path.isfile(path):
                    try:
                        with open(path, "r", encoding="utf-8") as fh:
                            lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip()]
                        if lines and lines[0].isdigit():
                            port = int(lines[0])
                            if 0 < port <= 65535:
                                return "127.0.0.1", port
                    except OSError:
                        pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        raise CdpError(
            "No DevToolsActivePort found under ~/.config/google-chrome or ~/.config/chromium. "
            "Ensure Droidgravity's CDP proxy is running."
        )

    @staticmethod
    def _candidate_port_files() -> List[str]:
        roots = []
        home = os.environ.get("HOME", "")
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg:
            roots.append(xdg)
        if home:
            roots.append(os.path.join(home, ".config"))
        files: List[str] = []
        for root in roots:
            files.append(os.path.join(root, "google-chrome", "DevToolsActivePort"))
            files.append(os.path.join(root, "chromium", "DevToolsActivePort"))
        return files

    def _http_json(self, method: str, path: str, retries: int = 1) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            host, port = self.discover_host_port()
            conn = http.client.HTTPConnection(host, port, timeout=12)
            try:
                conn.request(method, path)
                resp = conn.getresponse()
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status >= 400:
                    raise CdpError(f"HTTP {resp.status} for {method} {path}: {body}")
                if not body.strip():
                    return {}
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return {"text": body}
            except Exception as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(0.15)
            finally:
                conn.close()
        raise CdpError(f"CDP HTTP request failed ({method} {path}): {last_err}")

    def sync_pages(self) -> List[TargetState]:
        raw = self._http_json("GET", "/json/list")
        if not isinstance(raw, list):
            raw = []

        with self._lock:
            seen_target_ids = set()
            ordered: List[TargetState] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type", "page") != "page":
                    continue
                tid = entry.get("id", "")
                if not tid:
                    continue
                seen_target_ids.add(tid)
                state = self._states.get(tid)
                if state is None:
                    pid = self._id_by_target.get(tid)
                    if pid is None:
                        pid = self._next_page_id
                        self._next_page_id += 1
                        self._id_by_target[tid] = pid
                    state = TargetState(page_id=pid, target_id=tid)
                    self._states[tid] = state
                state.url = entry.get("url") or state.url or "about:blank"
                state.title = entry.get("title") or state.title or ""
                ordered.append(state)

            stale = [tid for tid in self._states if tid not in seen_target_ids]
            for tid in stale:
                st = self._states.pop(tid)
                self._id_by_target.pop(tid, None)
                if st.client is not None:
                    st.client.close()

            ordered.sort(key=lambda s: s.page_id)
            valid_pids = {s.page_id for s in ordered}
            if self._selected_page_id not in valid_pids:
                self._selected_page_id = ordered[0].page_id if ordered else None
            return ordered

    def format_pages_response(self) -> Dict[str, Any]:
        states = self.sync_pages()
        lines = ["## Pages"]
        pages_struct: List[Dict[str, Any]] = []
        selected_pid = self._selected_page_id
        for st in states:
            sel = st.page_id == selected_pid
            suffix = " [selected]" if sel else ""
            lines.append(f"{st.page_id}: {st.url}{suffix}")
            pages_struct.append({
                "id": st.page_id,
                "url": st.url,
                "title": st.title,
                "selected": sel,
            })
        if not states:
            lines.append("(no open pages)")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "structuredContent": {"pages": pages_struct},
        }

    def _resolve_state(self, page_id: Optional[Any], auto_create: bool = False) -> TargetState:
        states = self.sync_pages()
        if not states:
            if not auto_create:
                raise CdpError("No open pages. Call new_page first.")
            self.new_page("about:blank", background=False)
            states = self.sync_pages()
            if not states:
                raise CdpError("Failed to create default browser page")

        pid: Optional[int] = None
        if page_id is not None:
            try:
                pid = int(page_id)
            except (TypeError, ValueError) as exc:
                raise CdpError(f"Invalid pageId: {page_id}") from exc
        else:
            pid = self._selected_page_id

        for st in states:
            if st.page_id == pid:
                return st
        if page_id is None:
            return states[0]
        raise CdpError(f"Page not found: {page_id}")

    def _ensure_client(self, state: TargetState) -> CdpWebSocketClient:
        with self._lock:
            if state.client is not None and state.client.is_alive():
                return state.client
            host, port = self.discover_host_port()
            ws_url = f"ws://{host}:{port}/devtools/page/{state.target_id}"
            client = CdpWebSocketClient(
                ws_url,
                event_handler=lambda method, params, st=state: self._on_cdp_event(st, method, params),
            )
            client.connect()
            state.client = client

        for domain in ("Page.enable", "Runtime.enable", "DOM.enable", "Network.enable", "Log.enable"):
            try:
                client.call(domain, {}, timeout=5.0)
            except Exception:
                pass
        return client

    def _on_cdp_event(self, state: TargetState, method: str, params: Dict[str, Any]) -> None:
        if method == "Page.loadEventFired":
            state.load_event.set()
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId") and frame.get("url"):
                state.url = frame["url"]
        elif method == "Page.javascriptDialogOpening":
            state.pending_dialog = {
                "type": params.get("type", "alert"),
                "message": params.get("message", ""),
                "defaultPrompt": params.get("defaultPrompt", ""),
            }
        elif method == "Page.javascriptDialogClosed":
            state.pending_dialog = None
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args") or []
            parts = []
            for a in args:
                if "value" in a:
                    parts.append(str(a["value"]))
                elif "description" in a:
                    parts.append(str(a["description"]))
                else:
                    parts.append(a.get("type", "unknown"))
            mid = state.next_console_id
            state.next_console_id += 1
            state.console_messages.append({
                "msgid": mid,
                "type": params.get("type", "log"),
                "text": " ".join(parts),
                "stackTrace": params.get("stackTrace"),
                "timestamp": params.get("timestamp"),
            })
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exc = details.get("exception") or {}
            text = exc.get("description") or details.get("text") or "Uncaught exception"
            mid = state.next_console_id
            state.next_console_id += 1
            state.console_messages.append({
                "msgid": mid,
                "type": "error",
                "text": text,
                "stackTrace": details.get("stackTrace"),
                "timestamp": params.get("timestamp"),
            })
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            mid = state.next_console_id
            state.next_console_id += 1
            state.console_messages.append({
                "msgid": mid,
                "type": entry.get("level", "info"),
                "text": entry.get("text", ""),
                "url": entry.get("url"),
                "lineNumber": entry.get("lineNumber"),
            })
        elif method == "Network.requestWillBeSent":
            cdp_req_id = params.get("requestId", "")
            req = params.get("request") or {}
            if not cdp_req_id:
                return
            rec = state.network_by_id.get(cdp_req_id)
            if rec is None:
                rid = state.next_network_id
                state.next_network_id += 1
                rec = {
                    "reqid": rid,
                    "requestId": cdp_req_id,
                    "url": req.get("url", ""),
                    "method": req.get("method", "GET"),
                    "resourceType": params.get("type", "Other"),
                    "requestHeaders": req.get("headers") or {},
                    "postData": req.get("postData"),
                    "status": None,
                    "statusText": "",
                    "responseHeaders": {},
                    "mimeType": "",
                    "failed": False,
                    "errorText": "",
                }
                state.network_by_id[cdp_req_id] = rec
                state.network_requests.append(rec)
                # Bound auxiliary lookup dictionary
                while len(state.network_by_id) > 600:
                    oldest_key = next(iter(state.network_by_id))
                    state.network_by_id.pop(oldest_key, None)
        elif method == "Network.responseReceived":
            cdp_req_id = params.get("requestId", "")
            rec = state.network_by_id.get(cdp_req_id)
            if rec is not None:
                resp = params.get("response") or {}
                rec["status"] = resp.get("status")
                rec["statusText"] = resp.get("statusText", "")
                rec["responseHeaders"] = resp.get("headers") or {}
                rec["mimeType"] = resp.get("mimeType", "")
                if params.get("type"):
                    rec["resourceType"] = params["type"]
        elif method == "Network.loadingFailed":
            cdp_req_id = params.get("requestId", "")
            rec = state.network_by_id.get(cdp_req_id)
            if rec is not None:
                rec["failed"] = True
                rec["errorText"] = params.get("errorText", "failed")

    def select_page(self, page_id: int, bring_to_front: bool = True) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=False)
        self._http_json("GET", f"/json/activate/{st.target_id}")
        with self._lock:
            self._selected_page_id = st.page_id
        if bring_to_front:
            try:
                self._ensure_client(st).call("Page.bringToFront", {}, timeout=3.0)
            except Exception:
                pass
        return self.format_pages_response()

    def new_page(self, url: str = "about:blank", background: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
        target_url = (url or "about:blank").strip() or "about:blank"
        encoded = urllib.parse.quote(target_url, safe="")
        created = self._http_json("PUT", f"/json/new?url={encoded}")
        tid = created.get("id", "") if isinstance(created, dict) else ""
        states = self.sync_pages()
        st = next((s for s in states if s.target_id == tid), states[-1] if states else None)
        if st is not None:
            if not background:
                with self._lock:
                    self._selected_page_id = st.page_id
                try:
                    self._http_json("GET", f"/json/activate/{st.target_id}")
                except Exception:
                    pass
            if target_url != "about:blank":
                try:
                    client = self._ensure_client(st)
                    self._wait_document_ready(client, timeout=min(max(timeout, 1.0), 15.0))
                except Exception:
                    pass
        return self.format_pages_response()

    def close_page(self, page_id: int) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=False)
        self._http_json("GET", f"/json/close/{st.target_id}")
        return self.format_pages_response()

    def navigate_page(
        self,
        nav_type: Optional[str] = None,
        url: Optional[str] = None,
        ignore_cache: bool = False,
        timeout: float = 30.0,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        kind = nav_type or ("url" if url else "reload")
        timeout_sec = max((timeout or 30000) / 1000.0 if (timeout or 0) > 1000 else (timeout or 30.0), 2.0)

        st.load_event.clear()
        if kind == "url":
            if not url:
                raise CdpError("url is required when type is 'url'")
            nav = client.call("Page.navigate", {"url": url}, timeout=timeout_sec)
            if nav.get("errorText"):
                raise CdpError(f"Navigation failed: {nav['errorText']}")
            st.url = url
            self._wait_document_ready(client, st.load_event, timeout_sec)
        elif kind == "reload":
            client.call("Page.reload", {"ignoreCache": bool(ignore_cache)}, timeout=timeout_sec)
            self._wait_document_ready(client, st.load_event, timeout_sec)
        elif kind in ("back", "forward"):
            hist = client.call("Page.getNavigationHistory", {}, timeout=5.0)
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries") or []
            target_idx = idx - 1 if kind == "back" else idx + 1
            if 0 <= target_idx < len(entries):
                entry_id = entries[target_idx]["id"]
                client.call("Page.navigateToHistoryEntry", {"entryId": entry_id}, timeout=timeout_sec)
                self._wait_document_ready(client, st.load_event, timeout_sec)
            else:
                js = "history.back()" if kind == "back" else "history.forward()"
                self._eval_raw(client, js)
                time.sleep(0.25)
        else:
            raise CdpError(f"Unsupported navigate_page type: {kind}")

        self.sync_pages()
        cur_url = self._eval_raw(client, "location.href")
        if isinstance(cur_url, str) and cur_url:
            st.url = cur_url
        return self._text_result(f"Navigated page {st.page_id} ({kind}) to {st.url}")

    def _wait_document_ready(
        self,
        client: CdpWebSocketClient,
        load_event: Optional[threading.Event] = None,
        timeout: float = 15.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        if load_event is not None:
            load_event.wait(min(timeout, 5.0))
        while time.monotonic() < deadline:
            try:
                rs = self._eval_raw(client, "document.readyState")
                if rs in ("complete", "interactive"):
                    if rs == "complete":
                        return
                    time.sleep(0.15)
                    rs2 = self._eval_raw(client, "document.readyState")
                    if rs2 == "complete":
                        return
            except Exception:
                pass
            time.sleep(0.15)

    def _eval_raw(self, client: CdpWebSocketClient, expression: str, timeout: float = 15.0) -> Any:
        res = client.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            exc = details.get("exception") or {}
            msg = exc.get("description") or exc.get("value") or details.get("text") or "JavaScript exception"
            raise CdpError(msg)
        remote = res.get("result") or {}
        if remote.get("type") == "undefined":
            return None
        return remote.get("value")

    def take_snapshot(
        self,
        verbose: bool = False,
        file_path: Optional[str] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        snapshot_text = self._eval_raw(client, SNAPSHOT_JS, timeout=15.0) or ""
        if file_path:
            out_path = os.path.expanduser(file_path)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(snapshot_text)
            return self._text_result(f"Saved page snapshot to {out_path}\n\n{snapshot_text}")
        return self._text_result(snapshot_text)

    def take_screenshot(
        self,
        fmt: str = "png",
        quality: Optional[int] = None,
        full_page: bool = False,
        uid: Optional[str] = None,
        file_path: Optional[str] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        fmt_norm = fmt if fmt in ("png", "jpeg", "webp") else "png"

        params: Dict[str, Any] = {"format": fmt_norm, "fromSurface": True}
        if quality is not None and fmt_norm in ("jpeg", "webp"):
            params["quality"] = max(0, min(100, int(quality)))

        if uid:
            box = self._resolve_element_box(client, uid, scroll=True)
            params["clip"] = {
                "x": max(0.0, float(box["left"])),
                "y": max(0.0, float(box["top"])),
                "width": max(1.0, float(box["width"])),
                "height": max(1.0, float(box["height"])),
                "scale": 1.0,
            }
        elif full_page:
            metrics = client.call("Page.getLayoutMetrics", {}, timeout=5.0)
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            w = max(1.0, float(size.get("width", 1080)))
            h = min(16384.0, max(1.0, float(size.get("height", 1920))))
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0.0, "y": 0.0, "width": w, "height": h, "scale": 1.0}

        # Nudge compositor frame before capture so offscreen WebView has fresh paint
        try:
            self._eval_raw(client, "new Promise(r => requestAnimationFrame(() => r(true)))", timeout=2.0)
        except Exception:
            pass

        shot = client.call("Page.captureScreenshot", params, timeout=20.0)
        b64 = shot.get("data", "")
        if not b64:
            raise CdpError("Page.captureScreenshot returned empty image data")

        if file_path:
            out_path = os.path.expanduser(file_path)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(base64.b64decode(b64))
            return self._text_result(f"Saved screenshot to {out_path}")

        mime = f"image/{fmt_norm}"
        return {
            "content": [
                {"type": "text", "text": "Took a screenshot of the current page."},
                {"type": "image", "data": b64, "mimeType": mime},
            ]
        }

    def evaluate_script(
        self,
        function_src: str,
        args: Optional[List[Dict[str, Any]]] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        arg_specs = args or []
        arg_exprs: List[str] = []
        for item in arg_specs:
            if isinstance(item, dict) and "uid" in item:
                u = json.dumps(str(item["uid"]))
                arg_exprs.append(f"(window.__agy_snapshot_refs || {{}})[{u}]")
            else:
                arg_exprs.append(json.dumps(item))

        expr = f"(async () => {{ const __fn = ({function_src}); return await __fn({', '.join(arg_exprs)}); }})()"
        val = self._eval_raw(client, expr, timeout=20.0)
        json_str = json.dumps(val, ensure_ascii=False)
        return self._text_result(f"Script ran on page and returned:\n```json\n{json_str}\n```")

    def _resolve_element_box(self, client: CdpWebSocketClient, uid: str, scroll: bool = True) -> Dict[str, Any]:
        js = f"""
        (() => {{
          const refs = window.__agy_snapshot_refs || {{}};
          const el = refs[{json.dumps(uid)}];
          if (!el || !el.isConnected) {{
            return {{ ok: false, error: 'Element uid not found in current snapshot: ' + {json.dumps(uid)} }};
          }}
          if ({'true' if scroll else 'false'} && typeof el.scrollIntoView === 'function') {{
            el.scrollIntoView({{ block: 'center', inline: 'center', behavior: 'instant' }});
          }}
          const r = el.getBoundingClientRect();
          let ox = 0, oy = 0;
          try {{
            let w = el.ownerDocument.defaultView;
            while (w && w !== window && w.frameElement) {{
              const fr = w.frameElement.getBoundingClientRect();
              ox += fr.left;
              oy += fr.top;
              w = w.parent;
            }}
          }} catch (e) {{}}
          return {{
            ok: true,
            tag: el.tagName.toLowerCase(),
            left: r.left + ox,
            top: r.top + oy,
            width: r.width,
            height: r.height,
            cx: r.left + ox + r.width / 2,
            cy: r.top + oy + r.height / 2,
          }};
        }})()
        """
        info = self._eval_raw(client, js, timeout=10.0)
        if not isinstance(info, dict) or not info.get("ok"):
            raise CdpError((info or {}).get("error", f"Failed to resolve element {uid}"))
        return info

    def _dispatch_click_xy(self, client: CdpWebSocketClient, x: float, y: float, dbl_click: bool = False) -> None:
        client.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0
        }, timeout=5.0)
        clicks = 2 if dbl_click else 1
        for i in range(1, clicks + 1):
            client.call("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": i
            }, timeout=5.0)
            client.call("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": i
            }, timeout=5.0)

    def click(
        self,
        uid: str,
        dbl_click: bool = False,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        box = self._resolve_element_box(client, uid, scroll=True)
        self._dispatch_click_xy(client, float(box["cx"]), float(box["cy"]), dbl_click=dbl_click)
        time.sleep(0.1)
        return self._maybe_with_snapshot(
            st, f"Successfully clicked element {uid}.", include_snapshot
        )

    def click_at(
        self,
        x: float,
        y: float,
        dbl_click: bool = False,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        self._dispatch_click_xy(client, float(x), float(y), dbl_click=dbl_click)
        time.sleep(0.1)
        return self._maybe_with_snapshot(
            st, f"Successfully clicked at ({x}, {y}).", include_snapshot
        )

    def hover(
        self,
        uid: str,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        box = self._resolve_element_box(client, uid, scroll=True)
        client.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": float(box["cx"]),
            "y": float(box["cy"]),
            "button": "none",
            "buttons": 0,
        }, timeout=5.0)
        return self._maybe_with_snapshot(
            st, f"Successfully hovered over element {uid}.", include_snapshot
        )

    def fill(
        self,
        uid: str,
        value: str,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        box = self._resolve_element_box(client, uid, scroll=True)
        val_str = str(value)

        if box.get("tag") == "select":
            js_select = f"""
            (() => {{
              const el = (window.__agy_snapshot_refs || {{}})[{json.dumps(uid)}];
              if (!el) return {{ ok: false, error: 'Element missing' }};
              const target = {json.dumps(val_str)};
              for (const opt of el.options) {{
                if (opt.value === target || opt.text.trim() === target) {{
                  el.value = opt.value;
                  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                  return {{ ok: true }};
                }}
              }}
              return {{ ok: false, error: 'Option not found: ' + target }};
            }})()
            """
            res = self._eval_raw(client, js_select, timeout=5.0)
            if not (res or {}).get("ok"):
                raise CdpError((res or {}).get("error", "Failed selecting option"))
            return self._maybe_with_snapshot(st, f"Selected {val_str!r} on {uid}.", include_snapshot)

        # Focus element natively via CDP click + clear existing text safely
        self._dispatch_click_xy(client, float(box["cx"]), float(box["cy"]), dbl_click=False)
        clear_js = f"""
        (() => {{
          const el = (window.__agy_snapshot_refs || {{}})[{json.dumps(uid)}];
          if (!el) return false;
          el.focus();
          if (el.isContentEditable) {{
            el.textContent = '';
          }} else if ('value' in el) {{
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, '');
            else el.value = '';
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
          }}
          return true;
        }})()
        """
        self._eval_raw(client, clear_js, timeout=5.0)

        try:
            client.call("Input.insertText", {"text": val_str}, timeout=5.0)
        except Exception:
            pass

        verify_js = f"""
        (() => {{
          const el = (window.__agy_snapshot_refs || {{}})[{json.dumps(uid)}];
          if (!el) return false;
          const wanted = {json.dumps(val_str)};
          if (!el.isContentEditable && 'value' in el && el.value !== wanted) {{
            const proto = el.tagName === 'TEXTAREA'
              ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, wanted);
            else el.value = wanted;
          }}
          el.dispatchEvent(new Event('input', {{ bubbles: true }}));
          el.dispatchEvent(new Event('change', {{ bubbles: true }}));
          return true;
        }})()
        """
        self._eval_raw(client, verify_js, timeout=5.0)
        return self._maybe_with_snapshot(st, f"Successfully filled element {uid}.", include_snapshot)

    def fill_form(
        self,
        elements: List[Dict[str, Any]],
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        for item in elements:
            self.fill(str(item["uid"]), str(item["value"]), include_snapshot=False, page_id=page_id)
        st = self._resolve_state(page_id, auto_create=False)
        return self._maybe_with_snapshot(
            st, f"Successfully filled {len(elements)} form element(s).", include_snapshot
        )

    def press_key(
        self,
        key: str,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        parts = [p.strip() for p in key.split("+") if p.strip()]
        if not parts:
            raise CdpError("key must not be empty")

        modifiers = 0
        key_token = parts[-1]
        for mod in parts[:-1]:
            ml = mod.lower()
            if ml in ("alt",):
                modifiers |= 1
            elif ml in ("control", "ctrl"):
                modifiers |= 2
            elif ml in ("meta", "cmd", "command", "super"):
                modifiers |= 4
            elif ml in ("shift",):
                modifiers |= 8

        low = key_token.lower()
        if low in SPECIAL_KEYS:
            key_name, code, vk = SPECIAL_KEYS[low]
            text = "\r" if key_name == "Enter" else (" " if key_name == "Space" or key_name == " " else "")
        elif len(key_token) == 1:
            key_name = key_token
            ch = key_token.upper()
            vk = ord(ch)
            code = f"Key{ch}" if ch.isalpha() else (f"Digit{ch}" if ch.isdigit() else key_token)
            text = key_token if modifiers in (0, 8) else ""
        else:
            key_name = key_token
            code = key_token
            vk = 0
            text = ""

        down_type = "keyDown" if text else "rawKeyDown"
        down_params: Dict[str, Any] = {
            "type": down_type,
            "key": key_name,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if text:
            down_params["text"] = text
            down_params["unmodifiedText"] = text.lower()
        client.call("Input.dispatchKeyEvent", down_params, timeout=5.0)
        client.call("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": key_name,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }, timeout=5.0)
        time.sleep(0.05)
        return self._maybe_with_snapshot(st, f"Successfully pressed key: {key}.", include_snapshot)

    def drag(
        self,
        from_uid: str,
        to_uid: str,
        include_snapshot: bool = False,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        b1 = self._resolve_element_box(client, from_uid, scroll=True)
        b2 = self._resolve_element_box(client, to_uid, scroll=False)
        x1, y1 = float(b1["cx"]), float(b1["cy"])
        x2, y2 = float(b2["cx"]), float(b2["cy"])

        client.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x1, "y": y1, "button": "none", "buttons": 0
        }, timeout=5.0)
        client.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x1, "y": y1, "button": "left", "buttons": 1, "clickCount": 1
        }, timeout=5.0)
        for step in range(1, 6):
            t = step / 5.0
            client.call("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": x1 + (x2 - x1) * t,
                "y": y1 + (y2 - y1) * t,
                "button": "left",
                "buttons": 1,
            }, timeout=5.0)
        client.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x2, "y": y2, "button": "left", "buttons": 0, "clickCount": 1
        }, timeout=5.0)
        return self._maybe_with_snapshot(
            st, f"Dragged element {from_uid} to {to_uid}.", include_snapshot
        )

    def wait_for(
        self,
        text: Any,
        timeout: Optional[float] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        targets = [text] if isinstance(text, str) else [str(t) for t in (text or [])]
        targets = [t for t in targets if t]
        if not targets:
            raise CdpError("text must be a non-empty string or list of strings")

        timeout_ms = float(timeout) if timeout is not None else 10000.0
        timeout_sec = max(timeout_ms / 1000.0 if timeout_ms > 100 else timeout_ms, 0.2)
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            body_text = self._eval_raw(client, "document.body ? document.body.innerText : ''", timeout=5.0) or ""
            for needle in targets:
                if needle in body_text:
                    return self._text_result(f"Element with text {needle!r} found.")
            time.sleep(0.15)
        raise CdpError(f"Timed out waiting for text: {targets}")

    def resize_page(self, width: int, height: int, page_id: Optional[Any] = None) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        client = self._ensure_client(st)
        w = max(100, int(width))
        h = max(100, int(height))
        client.call("Emulation.setDeviceMetricsOverride", {
            "width": w,
            "height": h,
            "deviceScaleFactor": 1,
            "mobile": w < 768,
        }, timeout=5.0)
        return self._text_result(f"Resized page viewport to {w}x{h}.")

    def handle_dialog(
        self,
        action: str,
        prompt_text: Optional[str] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=False)
        client = self._ensure_client(st)
        accept = action == "accept"
        params: Dict[str, Any] = {"accept": accept}
        if prompt_text is not None:
            params["promptText"] = prompt_text

        try:
            client.call("Page.handleJavaScriptDialog", params, timeout=5.0)
            st.pending_dialog = None
            return self._text_result(f"Dialog {action}ed via CDP.")
        except CdpError:
            # Fallback to SystemGatewayServer WebChromeClient pending JsResult if WebView intercepted it
            gw_resp = self._call_gateway_dialog(st.target_id, accept, prompt_text)
            if gw_resp.get("ok"):
                st.pending_dialog = None
                return self._text_result(f"Dialog {action}ed via Android WebView handler.")
            raise CdpError("No active JavaScript dialog to handle")

    def _call_gateway_dialog(
        self, target_id: str, accept: bool, prompt_text: Optional[str]
    ) -> Dict[str, Any]:
        home = os.environ.get("HOME", "")
        port_file = os.path.join(home, "logs", "gateway.port") if home else ""
        if not port_file or not os.path.isfile(port_file):
            return {"ok": False}
        try:
            with open(port_file, "r", encoding="utf-8") as fh:
                gw_port = int(fh.read().strip())
        except Exception:
            return {"ok": False}

        conn = http.client.HTTPConnection("127.0.0.1", gw_port, timeout=3)
        try:
            payload: Dict[str, Any] = {
                "action": "dialog",
                "targetId": target_id,
                "accept": accept,
            }
            if prompt_text is not None:
                payload["promptText"] = prompt_text
            body = json.dumps({"command": "cdp_tab", "payload": payload}).encode("utf-8")
            conn.request("POST", "/gateway", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return {"ok": False}
        finally:
            conn.close()

    def list_console_messages(
        self,
        types: Optional[List[str]] = None,
        page_size: Optional[int] = None,
        page_idx: Optional[int] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        self._ensure_client(st)
        items = list(st.console_messages)
        if types:
            allowed = set(types)
            items = [m for m in items if m.get("type") in allowed]
        items = self._paginate(items, page_size, page_idx)
        if not items:
            return self._text_result("No console messages found.")
        lines = ["## Console messages"]
        for m in items:
            lines.append(f"msgid={m['msgid']} [{m['type']}] {m['text']}")
        return self._text_result("\n".join(lines))

    def get_console_message(self, msgid: int, page_id: Optional[Any] = None) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=False)
        for m in st.console_messages:
            if m["msgid"] == int(msgid):
                return self._text_result(json.dumps(m, ensure_ascii=False, indent=2))
        raise CdpError(f"Console message not found: {msgid}")

    def list_network_requests(
        self,
        resource_types: Optional[List[str]] = None,
        page_size: Optional[int] = None,
        page_idx: Optional[int] = None,
        page_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=True)
        self._ensure_client(st)
        items = list(st.network_requests)
        if resource_types:
            allowed = {r.lower() for r in resource_types}
            items = [r for r in items if (r.get("resourceType") or "").lower() in allowed]
        items = self._paginate(items, page_size, page_idx)
        if not items:
            return self._text_result("No network requests recorded.")
        lines = ["## Network requests"]
        for r in items:
            status = r.get("status")
            status_tag = f"[{status}]" if status is not None else ("[failed]" if r.get("failed") else "[pending]")
            lines.append(f"reqid={r['reqid']} {r['method']} {r['url']} {status_tag}")
        return self._text_result("\n".join(lines))

    def get_network_request(self, reqid: int, page_id: Optional[Any] = None) -> Dict[str, Any]:
        st = self._resolve_state(page_id, auto_create=False)
        client = self._ensure_client(st)
        for r in st.network_requests:
            if r["reqid"] == int(reqid):
                detail = dict(r)
                try:
                    body_res = client.call(
                        "Network.getResponseBody", {"requestId": r["requestId"]}, timeout=5.0
                    )
                    detail["responseBody"] = body_res.get("body", "")
                    detail["base64Encoded"] = bool(body_res.get("base64Encoded", False))
                except Exception:
                    pass
                return self._text_result(json.dumps(detail, ensure_ascii=False, indent=2))
        raise CdpError(f"Network request not found: {reqid}")

    def _maybe_with_snapshot(self, st: TargetState, msg: str, include_snapshot: bool) -> Dict[str, Any]:
        if not include_snapshot:
            return self._text_result(msg)
        snap = self.take_snapshot(page_id=st.page_id)
        snap_text = snap["content"][0]["text"]
        return self._text_result(f"{msg}\n\n{snap_text}")

    @staticmethod
    def _paginate(items: List[Any], page_size: Optional[int], page_idx: Optional[int]) -> List[Any]:
        if page_size is None:
            return items
        ps = max(1, int(page_size))
        pi = max(0, int(page_idx or 0))
        start = pi * ps
        return items[start : start + ps]

    @staticmethod
    def _text_result(text: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def close(self) -> None:
        with self._lock:
            for st in self._states.values():
                if st.client is not None:
                    st.client.close()
            self._states.clear()


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "list_pages",
        "description": "Get a list of pages open in the browser.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "select_page",
        "description": "Select a page as a context for future tool calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageId": {"type": "integer", "description": "The ID of the page to select."},
                "bringToFront": {"type": "boolean"},
            },
            "required": ["pageId"],
        },
    },
    {
        "name": "new_page",
        "description": "Creates a new page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to load in a new page."},
                "background": {"type": "boolean"},
                "timeout": {"type": "integer"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "close_page",
        "description": "Closes the page by its index. The last open page cannot be closed? Closed safely if requested.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageId": {"type": "integer", "description": "The ID of the page to close."},
            },
            "required": ["pageId"],
        },
    },
    {
        "name": "navigate_page",
        "description": "Navigates the currently selected page to a URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["url", "back", "forward", "reload"]},
                "url": {"type": "string"},
                "ignoreCache": {"type": "boolean"},
                "timeout": {"type": "integer"},
                "pageId": {"type": "integer"},
            },
        },
    },
    {
        "name": "take_snapshot",
        "description": "Take a text snapshot of the currently selected page based on the a11y tree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verbose": {"type": "boolean"},
                "filePath": {"type": "string"},
                "pageId": {"type": "integer"},
            },
        },
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot of the page or element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["png", "jpeg", "webp"]},
                "quality": {"type": "integer"},
                "uid": {"type": "string"},
                "fullPage": {"type": "boolean"},
                "filePath": {"type": "string"},
                "pageId": {"type": "integer"},
            },
        },
    },
    {
        "name": "evaluate_script",
        "description": "Evaluate a JavaScript function inside the currently selected page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string"},
                "args": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"uid": {"type": "string"}},
                        "required": ["uid"],
                    },
                },
                "pageId": {"type": "integer"},
            },
            "required": ["function"],
        },
    },
    {
        "name": "click",
        "description": "Clicks on the provided element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "dblClick": {"type": "boolean"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["uid"],
        },
    },
    {
        "name": "click_at",
        "description": "Clicks at the provided coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "dblClick": {"type": "boolean"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "hover",
        "description": "Hover over the provided element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["uid"],
        },
    },
    {
        "name": "fill",
        "description": "Type text into an input, text area, or select an option from a <select> element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string"},
                "value": {"type": "string"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["uid", "value"],
        },
    },
    {
        "name": "fill_form",
        "description": "Fill out multiple form elements at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "elements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "uid": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["uid", "value"],
                    },
                },
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["elements"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a key or key combination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "drag",
        "description": "Drag an element onto another element.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_uid": {"type": "string"},
                "to_uid": {"type": "string"},
                "includeSnapshot": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
            "required": ["from_uid", "to_uid"],
        },
    },
    {
        "name": "wait_for",
        "description": "Wait for the specified text to appear on the selected page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "timeout": {"type": "integer"},
                "pageId": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "resize_page",
        "description": "Resizes the selected page's window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "pageId": {"type": "integer"},
            },
            "required": ["width", "height"],
        },
    },
    {
        "name": "handle_dialog",
        "description": "If a browser dialog was opened, use this command to handle it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["accept", "dismiss"]},
                "promptText": {"type": "string"},
                "pageId": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "list_console_messages",
        "description": "List all console messages for the currently selected page since the last navigation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageSize": {"type": "integer"},
                "pageIdx": {"type": "integer"},
                "types": {"type": "array", "items": {"type": "string"}},
                "includePreservedMessages": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_console_message",
        "description": "Gets a console message by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "msgid": {"type": "integer"},
                "pageId": {"type": "integer"},
            },
            "required": ["msgid"],
        },
    },
    {
        "name": "list_network_requests",
        "description": "List all requests for the currently selected page since the last navigation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pageSize": {"type": "integer"},
                "pageIdx": {"type": "integer"},
                "resourceTypes": {"type": "array", "items": {"type": "string"}},
                "includePreservedRequests": {"type": "boolean"},
                "pageId": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_network_request",
        "description": "Gets a network request by an optional reqid.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reqid": {"type": "integer"},
                "pageId": {"type": "integer"},
            },
            "required": ["reqid"],
        },
    },
]


class McpServer:
    """MCP JSON-RPC 2.0 server over stdio."""

    def __init__(self, controller: BrowserController):
        self.controller = controller
        self._write_lock = threading.Lock()

    def dispatch_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        c = self.controller
        if name == "list_pages":
            return c.format_pages_response()
        if name == "select_page":
            return c.select_page(args["pageId"], bool(args.get("bringToFront", True)))
        if name == "new_page":
            return c.new_page(args.get("url", "about:blank"), bool(args.get("background", False)), float(args.get("timeout", 30)))
        if name == "close_page":
            return c.close_page(args["pageId"])
        if name == "navigate_page":
            return c.navigate_page(
                nav_type=args.get("type"),
                url=args.get("url"),
                ignore_cache=bool(args.get("ignoreCache", False)),
                timeout=float(args.get("timeout", 30000)),
                page_id=args.get("pageId"),
            )
        if name == "take_snapshot":
            return c.take_snapshot(bool(args.get("verbose", False)), args.get("filePath"), args.get("pageId"))
        if name == "take_screenshot":
            return c.take_screenshot(
                fmt=args.get("format", "png"),
                quality=args.get("quality"),
                full_page=bool(args.get("fullPage", False)),
                uid=args.get("uid"),
                file_path=args.get("filePath"),
                page_id=args.get("pageId"),
            )
        if name == "evaluate_script":
            return c.evaluate_script(args["function"], args.get("args"), args.get("pageId"))
        if name == "click":
            return c.click(args["uid"], bool(args.get("dblClick", False)), bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "click_at":
            return c.click_at(args["x"], args["y"], bool(args.get("dblClick", False)), bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "hover":
            return c.hover(args["uid"], bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "fill":
            return c.fill(args["uid"], args["value"], bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "fill_form":
            return c.fill_form(args.get("elements") or [], bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "press_key":
            return c.press_key(args["key"], bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "drag":
            return c.drag(args["from_uid"], args["to_uid"], bool(args.get("includeSnapshot", False)), args.get("pageId"))
        if name == "wait_for":
            return c.wait_for(args["text"], args.get("timeout"), args.get("pageId"))
        if name == "resize_page":
            return c.resize_page(args["width"], args["height"], args.get("pageId"))
        if name == "handle_dialog":
            return c.handle_dialog(args["action"], args.get("promptText"), args.get("pageId"))
        if name == "list_console_messages":
            return c.list_console_messages(args.get("types"), args.get("pageSize"), args.get("pageIdx"), args.get("pageId"))
        if name == "get_console_message":
            return c.get_console_message(args["msgid"], args.get("pageId"))
        if name == "list_network_requests":
            return c.list_network_requests(args.get("resourceTypes"), args.get("pageSize"), args.get("pageIdx"), args.get("pageId"))
        if name == "get_network_request":
            return c.get_network_request(args["reqid"], args.get("pageId"))
        raise CdpError(f"Unknown tool: {name}")

    def handle_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:
            # JSON-RPC notification
            return None

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2024-11-05"
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": proto,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "chrome-devtools-mcp", "version": "0.12.0-android"},
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOL_DEFINITIONS}}
            if method == "resources/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
            if method == "prompts/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": []}}
            if method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}
                try:
                    result = self.dispatch_tool(tool_name, tool_args)
                    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
                except Exception as exc:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": str(exc)}],
                            "isError": True,
                        },
                    }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    def serve_stdio(self, stdin_stream=None, stdout_stream=None) -> int:
        sin = stdin_stream or sys.stdin.buffer
        sout = stdout_stream or sys.stdout.buffer
        try:
            while True:
                line_bytes = sin.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    err_resp = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                    self._send_json(sout, err_resp)
                    continue

                resp = self.handle_message(msg)
                if resp is not None:
                    self._send_json(sout, resp)
        finally:
            self.controller.close()
        return 0

    def _send_json(self, sout, obj: Dict[str, Any]) -> None:
        raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            sout.write(raw)
            sout.flush()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Droidgravity Android WebView CDP MCP server")
    parser.add_argument("--autoConnect", "--auto-connect", action="store_true", default=False)
    parser.add_argument("--browserUrl", "--browser-url", dest="browser_url", default=None)
    parser.add_argument("--no-usage-statistics", action="store_true", default=False)
    parser.add_argument("--no-performance-crux", action="store_true", default=False)
    ns, _unknown = parser.parse_known_args(argv)
    return ns


def main(argv: Optional[List[str]] = None) -> int:
    ns = parse_args(argv)
    controller = BrowserController(browser_url=ns.browser_url)
    server = McpServer(controller)
    return server.serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
